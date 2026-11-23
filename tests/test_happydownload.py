"""Comprehensive unit & integration tests for Happydownload."""

from __future__ import annotations

from datetime import datetime
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request

from happydownload.adaptive import AdaptiveController
from happydownload.engine import DownloadManager
from happydownload.extractor import (
    _convert_ytdlp_entry,
    _extract_from_html_fallback,
    _unwrap_search_engine_video_page,
    inspect_any_url,
)
from happydownload.hls import parse_m3u8_text
from happydownload.models import DownloadStatus, SegmentStatus
from happydownload.network import FatalDownloadError
from happydownload.power import extract_download_url_candidate, is_in_full_speed_window
from happydownload.server import create_server
from happydownload.storage import PartFileHandlePool, SegmentStore


def make_deterministic_payload(size: int) -> bytes:
    """Create deterministic non-repeating bytes so offset bugs are caught immediately."""
    chunks = []
    block_size = 65536
    offset = 0
    counter = 0
    while offset < size:
        h = hashlib.sha256(f"block-{counter}".encode()).digest()
        # Expand 32 bytes deterministically up to block_size
        rep = (h * (block_size // len(h) + 1))[: min(block_size, size - offset)]
        chunks.append(rep)
        offset += len(rep)
        counter += 1
    return b"".join(chunks)


class MockMovieServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, payload: bytes, mode: str = "normal") -> None:
        self.payload = payload
        self.mode = mode  # normal | flaky | non_range | redirect_expiring
        self.request_count = 0
        self.lock = threading.Lock()
        self.token_valid = True
        super().__init__(("127.0.0.1", 0), MockMovieHandler)


class MockMovieHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args) -> None:
        return

    def do_HEAD(self) -> None:
        srv: MockMovieServer = self.server  # type: ignore[assignment]
        if srv.mode == "non_range":
            self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if srv.mode == "redirect_expiring" and self.path == "/movie.mkv":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/cdn/movie.mkv?token=v1")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", str(len(srv.payload)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", 'attachment; filename="test_movie.mkv"')
        self.send_header("ETag", '"movie-v1"')
        self.end_headers()

    def do_GET(self) -> None:
        srv: MockMovieServer = self.server  # type: ignore[assignment]
        with srv.lock:
            srv.request_count += 1
            req_no = srv.request_count

        if srv.mode == "redirect_expiring":
            if self.path == "/movie.mkv":
                tok = "v2" if not srv.token_valid else "v1"
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", f"/cdn/movie.mkv?token={tok}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/cdn/movie.mkv?token=v1" and req_no >= 4:
                srv.token_valid = False
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        if srv.mode == "flaky":
            if req_no in (2, 5):
                self.send_response(HTTPStatus.TOO_MANY_REQUESTS)
                self.send_header("Retry-After", "0.2")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

        range_hdr = self.headers.get("Range")
        if srv.mode == "non_range" or not range_hdr:
            data = srv.payload
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", 'attachment; filename="single_stream.mkv"')
            self.end_headers()
            self.wfile.write(data)
            return

        # Parse Range: bytes=start-end
        spec = range_hdr.replace("bytes=", "").strip()
        start_s, end_s = spec.split("-", 1)
        start = int(start_s)
        end = int(end_s) if end_s else len(srv.payload) - 1
        end = min(end, len(srv.payload) - 1)
        if start > end or start >= len(srv.payload):
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        chunk = srv.payload[start : end + 1]
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(srv.payload)}")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", 'attachment; filename="test_movie.mkv"')
        self.end_headers()

        if srv.mode == "flaky" and req_no in (3, 7) and len(chunk) > 4096:
            # Simulate sudden TCP disconnect mid-stream after sending part of chunk
            self.wfile.write(chunk[: len(chunk) // 3])
            self.wfile.flush()
            self.connection.close()
            return

        # Stream in 64 KiB slices
        pos = 0
        while pos < len(chunk):
            piece = chunk[pos : pos + 65536]
            self.wfile.write(piece)
            pos += len(piece)
            if srv.mode == "slow":
                time.sleep(0.015)


class TestHappydownload(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="happydl_test_")
        self.data_dir = os.path.join(self.tmp, "data")
        self.dl_dir = os.path.join(self.tmp, "downloads")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _new_manager(self) -> DownloadManager:
        mgr = DownloadManager(self.data_dir, self.dl_dir)
        mgr.update_settings({"desktop_notifications": False})
        return mgr

    def _wait_task(self, task, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while (
            task.is_running()
            or task.record.status in (DownloadStatus.QUEUED, DownloadStatus.PROBING)
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(task.is_running(), f"Task timed out in state {task.record.status}")

    def test_adaptive_controller_scales_and_backs_off(self) -> None:
        ctrl = AdaptiveController(
            initial_connections=2,
            min_connections=1,
            max_connections=8,
            adaptive_enabled=True,
            accept_ranges=True,
        )
        ctrl._last_eval_time = time.monotonic() - 5.0
        ctrl.record_bytes(2 * 1024 * 1024)
        t1 = ctrl.tick(active_workers=2, remaining_segments_exist=True)
        self.assertEqual(t1, 3)

        # Simulate big throughput improvement -> scales up further
        ctrl._last_eval_time = time.monotonic() - 5.0
        ctrl.record_bytes(6 * 1024 * 1024)
        t2 = ctrl.tick(active_workers=3, remaining_segments_exist=True)
        self.assertGreater(t2, t1)

        # Simulate throttle -> backs off immediately
        ctrl.report_throttle()
        t3 = ctrl.tick(active_workers=t2, remaining_segments_exist=True)
        self.assertLess(t3, t2)

    def test_multi_segment_download_and_sha256_integrity(self) -> None:
        payload = make_deterministic_payload(4 * 1024 * 1024 + 137)
        expected_sha = hashlib.sha256(payload).hexdigest()
        srv = MockMovieServer(payload, mode="normal")
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        mgr = self._new_manager()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/movie.mkv"
            task = mgr.create_download(
                url=url,
                expected_sha256=expected_sha,
                max_connections=6,
                initial_connections=3,
            )
            task.record.config.segment_size_bytes = 512 * 1024
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            self.assertEqual(task.record.actual_sha256, expected_sha)
            self.assertTrue(os.path.exists(task.record.dest_path))
            self.assertFalse(os.path.exists(task.record.part_path))
            self.assertFalse(os.path.exists(task.record.db_path))
            with open(task.record.dest_path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), expected_sha)
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_pause_restart_and_zero_loss_resume(self) -> None:
        payload = make_deterministic_payload(3 * 1024 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        srv = MockMovieServer(payload, mode="slow")
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        mgr1 = self._new_manager()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/slow_movie.mkv"
            task = mgr1.create_download(
                url=url,
                max_connections=4,
                initial_connections=2,
                speed_limit_bps=450 * 1024,
            )
            # Let some data checkpoint
            time.sleep(1.3)
            mgr1.pause(task.record.id)
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.PAUSED)
            saved_bytes = task.record.downloaded_bytes
            self.assertGreater(saved_bytes, 0)
            self.assertLess(saved_bytes, len(payload))
            dl_id = task.record.id
        finally:
            mgr1.shutdown()

        # Simulate app restart with a brand new DownloadManager reading persisted SQLite DB
        mgr2 = self._new_manager()
        try:
            resumed_task = mgr2.get_task(dl_id)
            self.assertIsNotNone(resumed_task)
            self.assertEqual(resumed_task.record.status, DownloadStatus.PAUSED)
            self.assertGreaterEqual(resumed_task.record.downloaded_bytes, saved_bytes)

            resumed_task.update_config(speed_limit_bps=0)
            srv.mode = "normal"
            self.assertTrue(mgr2.resume(dl_id))
            self._wait_task(resumed_task)
            self.assertEqual(resumed_task.record.status, DownloadStatus.COMPLETED)
            with open(resumed_task.record.dest_path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), expected_sha)
        finally:
            mgr2.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_flaky_network_drops_and_429_recovery(self) -> None:
        payload = make_deterministic_payload(2 * 1024 * 1024 + 55)
        expected_sha = hashlib.sha256(payload).hexdigest()
        srv = MockMovieServer(payload, mode="flaky")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/flaky.mkv"
            task = mgr.create_download(
                url=url,
                expected_sha256=expected_sha,
                max_connections=4,
                initial_connections=2,
            )
            self._wait_task(task, timeout=25.0)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            self.assertEqual(task.record.actual_sha256, expected_sha)
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_expiring_redirect_link_refreshes_seamlessly(self) -> None:
        payload = make_deterministic_payload(2 * 1024 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        srv = MockMovieServer(payload, mode="redirect_expiring")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/movie.mkv"
            task = mgr.create_download(url=url, max_connections=4, initial_connections=3)
            self._wait_task(task, timeout=20.0)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            with open(task.record.dest_path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), expected_sha)
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_non_range_single_stream_fallback(self) -> None:
        payload = make_deterministic_payload(1024 * 1024 + 19)
        expected_sha = hashlib.sha256(payload).hexdigest()
        srv = MockMovieServer(payload, mode="non_range")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/stream"
            task = mgr.create_download(url=url, max_connections=8)
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            self.assertFalse(task.record.accept_ranges)
            self.assertEqual(task.record.filename, "single_stream.mkv")
            with open(task.record.dest_path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), expected_sha)
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_web_ui_server_and_api(self) -> None:
        payload = make_deterministic_payload(512 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        mock_srv = MockMovieServer(payload, mode="normal")
        threading.Thread(target=mock_srv.serve_forever, daemon=True).start()

        mgr = self._new_manager()
        ui_srv = create_server(mgr, host="127.0.0.1", port=0)
        threading.Thread(target=ui_srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{ui_srv.server_address[1]}"
        try:
            # Verify static UI HTML serves properly
            with urllib.request.urlopen(f"{base}/") as resp:
                html = resp.read().decode("utf-8")
                self.assertIn("Happydownload", html)

            # Verify probe endpoint
            dl_url = f"http://127.0.0.1:{mock_srv.server_address[1]}/movie.mkv"
            req = urllib.request.Request(
                f"{base}/api/probe",
                data=json.dumps({"url": dl_url}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                probe_data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(probe_data["total_bytes"], len(payload))
                self.assertTrue(probe_data["accept_ranges"])
                self.assertTrue(probe_data["storage_ok"])

            # Start download via API
            req2 = urllib.request.Request(
                f"{base}/api/downloads",
                data=json.dumps({"url": dl_url, "expected_sha256": expected_sha}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req2) as resp:
                created = json.loads(resp.read().decode("utf-8"))
                dl_id = created["download"]["id"]

            task = mgr.get_task(dl_id)
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            self.assertEqual(task.record.actual_sha256, expected_sha)
        finally:
            ui_srv.shutdown()
            ui_srv.server_close()
            mgr.shutdown()
            mock_srv.shutdown()
            mock_srv.server_close()

    def test_work_stealing_and_sparse_60gb_ready(self) -> None:
        # 1) Verify SegmentStore work-stealing splits remaining tail cleanly without overlap
        db_path = os.path.join(self.tmp, "steal.happydl.db")
        ss = SegmentStore(db_path)
        ss.initialize_segments(total_bytes=20 * 1024 * 1024, accept_ranges=True, segment_size_bytes=20 * 1024 * 1024)
        seg1 = ss.claim_next_segment()
        self.assertIsNotNone(seg1)
        ss.checkpoint_segment(seg1.segment_id, 2 * 1024 * 1024)

        seg2 = ss.claim_next_segment(min_steal_remaining=2 * 1024 * 1024)
        self.assertIsNotNone(seg2)
        seg1_new_end = ss.get_segment_end(seg1.segment_id)
        self.assertEqual(seg1_new_end + 1, seg2.start_byte)
        self.assertEqual(seg2.end_byte, 20 * 1024 * 1024 - 1)
        ss.close()

        # 2) Verify Windows sparse file allocation works for 60 GB without zero-fill hang
        part_path = os.path.join(self.tmp, "movie_60gb.part")
        t0 = time.monotonic()
        pool = PartFileHandlePool(part_path, total_bytes=60 * 1024 * 1024 * 1024)
        elapsed = time.monotonic() - t0
        try:
            pool.write_at(59 * 1024 * 1024 * 1024, b"HAPPYDL_TAIL")
            pool.flush_thread_handle()
            self.assertLess(elapsed, 2.0)
        finally:
            pool.close_all()

    def test_keep_alive_reuse_across_segments(self) -> None:
        payload = make_deterministic_payload(2 * 1024 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        srv = MockMovieServer(payload, mode="normal")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/movie.mkv"
            task = mgr.create_download(url=url, max_connections=2, initial_connections=2)
            # Force small 256 KiB segments so each worker runs multiple consecutive segments
            task.record.config.segment_size_bytes = 256 * 1024
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            self.assertGreater(task.to_dict()["keepalive_reuses"], 0)
            with open(task.record.dest_path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), expected_sha)
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_update_url_and_mirror_failover_preserves_progress(self) -> None:
        payload = make_deterministic_payload(2 * 1024 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        srv1 = MockMovieServer(payload, mode="slow")
        srv2 = MockMovieServer(payload, mode="normal")
        threading.Thread(target=srv1.serve_forever, daemon=True).start()
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            url1 = f"http://127.0.0.1:{srv1.server_address[1]}/movie.mkv"
            url2 = f"http://127.0.0.1:{srv2.server_address[1]}/movie.mkv"
            task = mgr.create_download(url=url1, max_connections=2, initial_connections=2, speed_limit_bps=350 * 1024)
            time.sleep(1.0)
            mgr.pause(task.record.id)
            self._wait_task(task)
            saved = task.record.downloaded_bytes
            self.assertGreater(saved, 0)

            # Swap primary URL to srv2 while keeping all downloaded bytes
            task.update_url(url2, add_as_mirror=False)
            task.update_config(speed_limit_bps=0)
            self.assertTrue(mgr.resume(task.record.id))
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            with open(task.record.dest_path, "rb") as f:
                self.assertEqual(hashlib.sha256(f.read()).hexdigest(), expected_sha)
        finally:
            mgr.shutdown()
            srv1.shutdown()
            srv1.server_close()
            srv2.shutdown()
            srv2.server_close()

    def test_orphan_scan_and_clean(self) -> None:
        mgr = self._new_manager()
        try:
            orphan_part = os.path.join(self.dl_dir, "old_aborted.mkv.part")
            orphan_db = os.path.join(self.dl_dir, "old_aborted.mkv.part.happydl.db")
            real_movie = os.path.join(self.dl_dir, "completed_movie.mkv")
            with open(orphan_part, "wb") as f:
                f.write(b"0" * 4096)
            with open(orphan_db, "wb") as f:
                f.write(b"1" * 1024)
            with open(real_movie, "wb") as f:
                f.write(b"2" * 2048)

            orphans = mgr.scan_orphans()
            self.assertEqual(len(orphans), 2)
            res = mgr.clean_orphans()
            self.assertEqual(res["removed"], 2)
            self.assertEqual(res["freed_bytes"], 5120)
            self.assertTrue(os.path.exists(real_movie))
            self.assertFalse(os.path.exists(orphan_part))
        finally:
            mgr.shutdown()

    def test_queue_limit_settings_schedule_and_clipboard(self) -> None:
        # Schedule overnight helper tests
        self.assertTrue(is_in_full_speed_window("23:00", "07:00", datetime(2026, 11, 13, 23, 30)))
        self.assertTrue(is_in_full_speed_window("23:00", "07:00", datetime(2026, 11, 13, 3, 15)))
        self.assertFalse(is_in_full_speed_window("23:00", "07:00", datetime(2026, 11, 13, 14, 0)))

        # Clipboard link detection tests (supports both direct files and web video page links)
        self.assertEqual(
            extract_download_url_candidate("  https://cdn.example.com/movies/odyssey.mkv  "),
            "https://cdn.example.com/movies/odyssey.mkv",
        )
        self.assertEqual(
            extract_download_url_candidate("https://video.example.com/watch/12345"),
            "https://video.example.com/watch/12345",
        )
        self.assertIsNone(extract_download_url_candidate("http://127.0.0.1:8765/"))
        self.assertIsNone(extract_download_url_candidate("just some random notes"))

        # Queue concurrency & settings persistence
        payload = make_deterministic_payload(512 * 1024)
        srv = MockMovieServer(payload, mode="normal")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr1 = self._new_manager()
        try:
            mgr1.update_settings({"max_concurrent_downloads": 1, "desktop_notifications": False})
            u = f"http://127.0.0.1:{srv.server_address[1]}/movie.mkv"
            t1 = mgr1.create_download(url=u, filename="q1.mkv")
            t2 = mgr1.create_download(url=u, filename="q2.mkv")
            self._wait_task(t1)
            self._wait_task(t2)
            self.assertEqual(t1.record.status, DownloadStatus.COMPLETED)
            self.assertEqual(t2.record.status, DownloadStatus.COMPLETED)
            self.assertTrue(t1.record.db_path.startswith(mgr1.segments_dir))
        finally:
            mgr1.shutdown()
            srv.shutdown()
            srv.server_close()

        mgr2 = DownloadManager(self.data_dir, self.dl_dir)
        try:
            self.assertEqual(mgr2.get_settings()["max_concurrent_downloads"], 1)
            self.assertFalse(mgr2.get_settings()["desktop_notifications"])
        finally:
            mgr2.shutdown()

    def test_html_video_page_extraction_and_download(self) -> None:
        vid_720 = make_deterministic_payload(300 * 1024)
        vid_1080 = make_deterministic_payload(500 * 1024)
        expected_sha = hashlib.sha256(vid_1080).hexdigest()

        class VideoPageHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args) -> None:
                return

            def do_HEAD(self) -> None:
                if self.path == "/watch/sample":
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                data = vid_1080 if "1080" in self.path else vid_720
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self) -> None:
                if self.path == "/watch/sample":
                    html_doc = (
                        "<html><head><title>Awesome Sample Clip</title></head><body>"
                        '<video controls>'
                        '<source src="/media/clip_720p.mp4" type="video/mp4">'
                        '<source src="/media/clip_1080p.mp4" type="video/mp4">'
                        "</video></body></html>"
                    ).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(html_doc)))
                    self.end_headers()
                    self.wfile.write(html_doc)
                    return

                # Verify Referer was preserved for CDN playback
                if not self.headers.get("Referer"):
                    self.send_response(HTTPStatus.FORBIDDEN)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                data = vid_1080 if "1080" in self.path else vid_720
                rng = self.headers.get("Range")
                if rng:
                    s, e = rng.replace("bytes=", "").split("-", 1)
                    start = int(s)
                    end = int(e) if e else len(data) - 1
                    piece = data[start : end + 1]
                    self.send_response(HTTPStatus.PARTIAL_CONTENT)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                    self.send_header("Content-Length", str(len(piece)))
                    self.end_headers()
                    self.wfile.write(piece)
                    return

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), VideoPageHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            page_url = f"http://127.0.0.1:{srv.server_address[1]}/watch/sample"
            probe_res = mgr.probe(page_url)
            self.assertEqual(probe_res["kind"], "videos")
            self.assertGreaterEqual(len(probe_res["videos"]), 1)
            self.assertIn("Awesome Sample Clip", probe_res["videos"][0]["title"])
            self.assertGreaterEqual(len(probe_res["videos"][0]["formats"]), 2)

            # Also verify built-in zero-dependency HTML5 fallback extractor directly
            fallback_vids = _extract_from_html_fallback(page_url, user_agent="HappydownloadTest/1.0")
            self.assertEqual(len(fallback_vids), 1)
            self.assertEqual(fallback_vids[0].title, "Awesome Sample Clip")
            self.assertEqual(len(fallback_vids[0].formats), 2)

            tasks = mgr.create_downloads_from_url(page_url, max_connections=4)
            self.assertGreaterEqual(len(tasks), 1)
            for t in tasks:
                self._wait_task(t)
                self.assertEqual(t.record.status, DownloadStatus.COMPLETED)
            downloaded_hashes = set()
            for t in tasks:
                with open(t.record.dest_path, "rb") as f:
                    downloaded_hashes.add(hashlib.sha256(f.read()).hexdigest())
            self.assertIn(expected_sha, downloaded_hashes)
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_hls_master_playlist_parallel_download_and_pause_resume(self) -> None:
        segments = [make_deterministic_payload(64 * 1024 + idx * 17) for idx in range(10)]
        expected_bytes = b"".join(segments)

        class HlsHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/master.m3u8":
                    body = (
                        "#EXTM3U\n"
                        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=854x480\n"
                        "/low/index.m3u8\n"
                        "#EXT-X-STREAM-INF:BANDWIDTH=3500000,RESOLUTION=1920x1080\n"
                        "/hd/index.m3u8\n"
                    ).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/hd/index.m3u8":
                    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:4"]
                    for idx in range(len(segments)):
                        lines.append("#EXTINF:4.0,")
                        lines.append(f"/hd/seg_{idx}.ts")
                    lines.append("#EXT-X-ENDLIST")
                    body = "\n".join(lines).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/hd/seg_") and self.path.endswith(".ts"):
                    idx = int(self.path.split("_")[1].split(".")[0])
                    data = segments[idx]
                    time.sleep(0.05)
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Length", "0")
                self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), HlsHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}/master.m3u8"
            info = mgr.probe(url)
            self.assertEqual(info["kind"], "videos")
            self.assertEqual(info["videos"][0]["formats"][0]["height"], 1080)

            task = mgr.create_download(url=url, filename="hls_movie.ts", max_connections=4, initial_connections=2)
            time.sleep(0.18)
            mgr.pause(task.record.id)
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.PAUSED)

            self.assertTrue(mgr.resume(task.record.id))
            self._wait_task(task, timeout=20.0)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            with open(task.record.dest_path, "rb") as f:
                self.assertEqual(f.read(), expected_bytes)
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()

    def test_hls_drm_stream_rejected_cleanly(self) -> None:
        drm_m3u8 = (
            "#EXTM3U\n"
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://drm.example.com/key"\n'
            "#EXTINF:4.0,\n"
            "seg0.ts\n"
        )
        with self.assertRaises(FatalDownloadError):
            parse_m3u8_text("https://example.com/drm.m3u8", drm_m3u8)

    def test_html_page_without_video_rejected_not_downloaded_as_direct(self) -> None:
        class EmptyHtmlHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                return

            def do_HEAD(self) -> None:
                body = b"<html><title>Search Wrapper</title><body><div id='app'>Loading</div></body></html>"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()

            def do_GET(self) -> None:
                body = b"<html><title>Search Wrapper</title><body><div id='app'>Loading</div></body></html>"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        site_srv = ThreadingHTTPServer(("127.0.0.1", 0), EmptyHtmlHandler)
        threading.Thread(target=site_srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        api_srv = create_server(mgr, host="127.0.0.1", port=0)
        threading.Thread(target=api_srv.serve_forever, daemon=True).start()
        try:
            target_url = f"http://127.0.0.1:{site_srv.server_address[1]}/videos/riverview/relatedvideo?q=test"
            base = f"http://127.0.0.1:{api_srv.server_address[1]}"

            for endpoint in ("/api/probe", "/api/downloads"):
                req = urllib.request.Request(
                    f"{base}{endpoint}",
                    data=json.dumps({"url": target_url}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=10.0)
                self.assertEqual(ctx.exception.code, 400)
                try:
                    err_payload = json.loads(ctx.exception.read().decode("utf-8"))
                finally:
                    ctx.exception.close()
                self.assertIn("error", err_payload)
                self.assertIn("video", err_payload["error"].lower())
        finally:
            api_srv.shutdown()
            api_srv.server_close()
            mgr.shutdown()
            site_srv.shutdown()
            site_srv.server_close()

    def test_html_wrapper_unwraps_embedded_iframe_video(self) -> None:
        class WrapperHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args) -> None:
                return

            def do_HEAD(self) -> None:
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", "128")
                self.end_headers()

            def do_GET(self) -> None:
                if self.path == "/wrapper":
                    body = (
                        '<html><head><title>Wrapper Page</title></head>'
                        '<body><iframe src="/embed/player.html"></iframe></body></html>'
                    ).encode("utf-8")
                elif self.path == "/embed/player.html":
                    body = (
                        '<html><head><title>Embedded Clip</title></head>'
                        '<body><video src="/cdn/sample_720p.mp4"></video></body></html>'
                    ).encode("utf-8")
                else:
                    self.send_response(HTTPStatus.NOT_FOUND)
                    self.end_headers()
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        site_srv = ThreadingHTTPServer(("127.0.0.1", 0), WrapperHandler)
        threading.Thread(target=site_srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{site_srv.server_address[1]}/wrapper"
            res = inspect_any_url(url, user_agent="HappydownloadTest/1.0")
            self.assertEqual(res.kind, "videos")
            self.assertTrue(res.videos)
            self.assertTrue(res.videos[0].formats[0].url.endswith("/cdn/sample_720p.mp4"))
            self.assertEqual(res.videos[0].formats[0].height, 720)
        finally:
            site_srv.shutdown()
            site_srv.server_close()

    def test_split_hd_video_and_audio_pairing_in_ytdlp_converter(self) -> None:
        mock_entry = {
            "id": "uhd-demo",
            "title": "4K Cinema Demo",
            "webpage_url": "https://example.com/watch/uhd-demo",
            "extractor_key": "Generic",
            "duration": 120,
            "formats": [
                {
                    "format_id": "140",
                    "url": "https://cdn.example.com/audio_192k.m4a",
                    "ext": "m4a",
                    "protocol": "https",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "abr": 192,
                    "filesize": 3_000_000,
                },
                {
                    "format_id": "18",
                    "url": "https://cdn.example.com/prog_360p.mp4",
                    "ext": "mp4",
                    "protocol": "https",
                    "vcodec": "avc1",
                    "acodec": "mp4a.40.2",
                    "height": 360,
                    "tbr": 600,
                    "filesize": 9_000_000,
                },
                {
                    "format_id": "313",
                    "url": "https://cdn.example.com/video_2160p_only.mp4",
                    "ext": "mp4",
                    "protocol": "https",
                    "vcodec": "avc1",
                    "acodec": "none",
                    "height": 2160,
                    "tbr": 18000,
                    "filesize": 250_000_000,
                },
            ],
        }
        vid = _convert_ytdlp_entry(mock_entry, "https://example.com/watch/uhd-demo", ffmpeg_available=True)
        self.assertIsNotNone(vid)
        assert vid is not None
        # Highest resolution (2160p 4K paired with audio) should be sorted first
        top_fmt = vid.formats[0]
        self.assertEqual(top_fmt.height, 2160)
        self.assertTrue(top_fmt.has_video and top_fmt.has_audio)
        self.assertEqual(top_fmt.audio_url, "https://cdn.example.com/audio_192k.m4a")
        self.assertEqual(top_fmt.filesize_approx, 253_000_000)
        self.assertIn("4K", top_fmt.label)
        self.assertIn("HD+Audio Merge", top_fmt.label)

    def test_search_wrapper_vrhm_and_query_redirect_unwrapping(self) -> None:
        html_body = (
            '<html><body>'
            '<div class="dg_u" vrhm="{&quot;mid&quot;:&quot;ABC123MID&quot;,'
            '&quot;murl&quot;:&quot;https://cdn.example.com/movie_1080p.mp4&quot;}"></div>'
            '</body></html>'
        )
        unwrapped = _unwrap_search_engine_video_page(
            "https://www.bing.com/videos/riverview/relatedvideo?q=test&mid=ABC123MID",
            html_body,
            user_agent="HappydownloadTest/1.0",
        )
        self.assertEqual(unwrapped, ["https://cdn.example.com/movie_1080p.mp4"])

        ig_unwrapped = _unwrap_search_engine_video_page(
            "https://www.instagram.com/reel/C9xYz123AbC/?igsh=test",
            "<html><body>Instagram</body></html>",
            user_agent="HappydownloadTest/1.0",
        )
        self.assertEqual(ig_unwrapped, ["https://www.instagram.com/p/C9xYz123AbC/embed/captioned/"])

    def test_auto_categorize_folders_and_clear_completed(self) -> None:
        payload = make_deterministic_payload(128 * 1024)
        srv = MockMovieServer(payload, mode="normal")
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mgr = self._new_manager()
        try:
            updated = mgr.update_settings({"auto_categorize_folders": True, "cookies_from_browser": "edge"})
            self.assertEqual(updated["cookies_from_browser"], "edge")
            url = f"http://127.0.0.1:{srv.server_address[1]}/clip.mp4"
            task = mgr.create_download(url=url, filename="clip.mp4", max_connections=2, initial_connections=2)
            self._wait_task(task)
            self.assertEqual(task.record.status, DownloadStatus.COMPLETED)
            self.assertEqual(os.path.basename(task.record.dest_dir), "Video")
            self.assertTrue(os.path.exists(task.record.dest_path))

            cleared = mgr.clear_completed()
            self.assertEqual(cleared, 1)
            self.assertEqual(len(mgr.list_tasks()), 0)
            # Completed file must remain untouched on disk after clear_completed
            self.assertTrue(os.path.exists(task.record.dest_path))
        finally:
            mgr.shutdown()
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
