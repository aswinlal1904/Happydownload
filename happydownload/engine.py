"""Core adaptive multi-segment download engine, queue scheduler, and manager."""

from __future__ import annotations

from collections import deque
import http.client
import os
import random
import shutil
import socket
import ssl
import threading
import time
import uuid
from typing import Callable, Deque, Dict, List, Optional

from .adaptive import AdaptiveController, TokenBucketLimiter
from .extractor import ensure_ffmpeg, find_ffmpeg, get_capabilities, inspect_any_url
from .hls import resolve_hls_media_playlist
from .models import (
    AppSettings,
    DownloadConfig,
    DownloadRecord,
    DownloadStatus,
    ExtractedVideo,
    ProbeResult,
    Segment,
    SegmentStatus,
    WorkerSnapshot,
)
from .network import (
    FatalDownloadError,
    KeepAliveClient,
    LinkExpiredError,
    ThrottleError,
    TransientNetworkError,
    open_range_stream,
    probe_url,
    sanitize_filename,
)
from .power import (
    execute_power_action,
    extract_download_url_candidate,
    is_in_full_speed_window,
    read_clipboard_text,
    set_prevent_sleep,
    show_desktop_notification,
)
from .storage import (
    DownloadStore,
    PartFileHandlePool,
    SegmentStore,
    check_storage_compatibility,
    compute_sha256_streaming,
)

ACTIVE_STATUSES = {
    DownloadStatus.PROBING,
    DownloadStatus.DOWNLOADING,
    DownloadStatus.MERGING,
    DownloadStatus.WAITING_NETWORK,
    DownloadStatus.VERIFYING,
}

_CATEGORY_MAP = {
    "Video": {".mp4", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv", ".m4v", ".ts"},
    "Audio": {".mp3", ".m4a", ".flac", ".wav", ".aac", ".ogg", ".opus"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2", ".iso", ".img"},
    "Programs": {".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk"},
    "Documents": {".pdf", ".epub", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"},
}


def _resolve_category_dir(base_dir: str, filename: str, enabled: bool) -> str:
    if not enabled or not filename:
        return base_dir
    ext = os.path.splitext(filename)[1].lower()
    for folder, exts in _CATEGORY_MAP.items():
        if ext in exts:
            target = os.path.join(base_dir, folder)
            os.makedirs(target, exist_ok=True)
            return target
    return base_dir


def _unique_dest_path(dest_dir: str, filename: str) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    safe_name = sanitize_filename(filename)
    candidate = os.path.join(dest_dir, safe_name)
    if not os.path.exists(candidate) and not os.path.exists(candidate + ".part"):
        return candidate
    stem, ext = os.path.splitext(safe_name)
    idx = 1
    while True:
        cand = os.path.join(dest_dir, f"{stem} ({idx}){ext}")
        if not os.path.exists(cand) and not os.path.exists(cand + ".part"):
            return cand
        idx += 1


class DownloadTask:
    """Manages the live execution, resilience, telemetry, and adaptive scaling of one download."""

    def __init__(
        self,
        record: DownloadRecord,
        store: DownloadStore,
        on_event: Optional[Callable[[str, Dict], None]] = None,
        needs_probe: bool = False,
        on_complete_cb: Optional[Callable[["DownloadTask"], None]] = None,
        on_finish_cb: Optional[Callable[["DownloadTask"], None]] = None,
    ) -> None:
        self.record = record
        self.store = store
        self._on_event = on_event
        self._needs_probe = needs_probe
        self._on_complete_cb = on_complete_cb
        self._on_finish_cb = on_finish_cb

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._cancel_requested = False
        self._thread: Optional[threading.Thread] = None

        self._adaptive: Optional[AdaptiveController] = None
        self._limiter = TokenBucketLimiter(record.config.speed_limit_bps)
        self._schedule_cap_bps: int = 0  # 0 = no schedule cap
        self._workers: Dict[int, WorkerSnapshot] = {}
        self._worker_threads: Dict[int, threading.Thread] = {}
        self._worker_errors: Dict[int, bool] = {}
        self._next_worker_id = 1

        self._seg_store: Optional[SegmentStore] = None
        self._part_pool: Optional[PartFileHandlePool] = None
        self._refresh_lock = threading.Lock()
        self._last_refresh_time = 0.0

        self._logs: Deque[Dict[str, object]] = deque(maxlen=80)
        self._verify_progress: float = 0.0
        self._segment_summary_cache: List[int] = []
        self._last_summary_ts: float = 0.0
        self._keepalive_reuses: int = 0

    def log(self, message: str, level: str = "info") -> None:
        entry = {"ts": time.time(), "level": level, "message": message}
        with self._lock:
            self._logs.append(entry)
        if self._on_event:
            self._on_event("log", {"id": self.record.id, **entry})

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._cancel_requested = False
            self._thread = threading.Thread(
                target=self._run_lifecycle,
                name=f"happydl-coord-{self.record.id[:8]}",
                daemon=True,
            )
            self._thread.start()

    def pause(self) -> None:
        self._stop_event.set()
        self.log("Pause requested — flushing active chunks safely", "info")

    def cancel(self) -> None:
        self._cancel_requested = True
        self._stop_event.set()
        self.log("Cancellation requested", "warn")

    def apply_effective_rate_limit(self, schedule_cap_bps: int = 0) -> None:
        self._schedule_cap_bps = max(0, int(schedule_cap_bps))
        user_cap = max(0, int(self.record.config.speed_limit_bps))
        if user_cap > 0 and self._schedule_cap_bps > 0:
            effective = min(user_cap, self._schedule_cap_bps)
        else:
            effective = user_cap or self._schedule_cap_bps
        self._limiter.set_rate(effective)

    def update_config(
        self,
        max_connections: Optional[int] = None,
        adaptive_enabled: Optional[bool] = None,
        speed_limit_bps: Optional[int] = None,
    ) -> None:
        cfg = self.record.config
        if max_connections is not None:
            cfg.max_connections = max(1, min(64, int(max_connections)))
            cfg.initial_connections = min(cfg.initial_connections, cfg.max_connections)
        if adaptive_enabled is not None:
            cfg.adaptive_enabled = bool(adaptive_enabled)
        if speed_limit_bps is not None:
            cfg.speed_limit_bps = max(0, int(speed_limit_bps))
            self.apply_effective_rate_limit(self._schedule_cap_bps)

        if self._adaptive is not None:
            self._adaptive.update_limits(
                cfg.min_connections,
                cfg.max_connections,
                cfg.adaptive_enabled,
            )
        self.store.save_record(self.record)
        self.log(
            f"Updated settings: max_conn={cfg.max_connections}, adaptive={cfg.adaptive_enabled}, "
            f"limit={'unlimited' if cfg.speed_limit_bps == 0 else f'{cfg.speed_limit_bps // 1024} KB/s'}"
        )

    def update_url(self, new_url: str, add_as_mirror: bool = False) -> ProbeResult:
        """Replace primary URL or append a verified backup mirror without losing downloaded bytes."""
        clean_url = new_url.strip()
        if not clean_url:
            raise FatalDownloadError("New URL cannot be empty")
        cfg = self.record.config
        probe = probe_url(
            clean_url,
            user_agent=cfg.user_agent,
            custom_headers=cfg.custom_headers,
            timeout=cfg.connect_timeout,
        )
        if (
            self.record.total_bytes is not None
            and probe.total_bytes is not None
            and probe.total_bytes != self.record.total_bytes
        ):
            raise FatalDownloadError(
                f"New link size ({probe.total_bytes} bytes) does not match existing download ({self.record.total_bytes} bytes)."
            )
        if add_as_mirror:
            if clean_url not in self.record.mirrors and clean_url != self.record.url:
                self.record.mirrors.append(clean_url)
            self.log(f"Added mirror URL ({len(self.record.mirrors)} total mirror(s))")
        else:
            self.record.url = clean_url
            self.record.final_url = probe.final_url
            if probe.etag:
                self.record.etag = probe.etag
            if probe.last_modified:
                self.record.last_modified = probe.last_modified
            self.record.error_message = None
            self.log("Download link updated — saved segments preserved for resume")
        self.store.save_record(self.record)
        return probe

    def _set_status(self, status: DownloadStatus, error: Optional[str] = None) -> None:
        self.record.status = status
        self.record.error_message = error
        self.store.save_record(self.record)

    def _validate_remote_identity(self, probe: ProbeResult) -> None:
        rec = self.record
        if rec.total_bytes is not None and probe.total_bytes is not None and probe.total_bytes != rec.total_bytes:
            raise FatalDownloadError(
                f"Remote file size changed ({rec.total_bytes} -> {probe.total_bytes}); aborting to prevent corruption."
            )
        if rec.downloaded_bytes > 0 and rec.etag and probe.etag and rec.etag != probe.etag:
            raise FatalDownloadError(
                f"Remote file ETag changed ({rec.etag} -> {probe.etag}); file on server was modified."
            )

    def _refresh_final_url(self) -> bool:
        """Re-probe primary URL and fallback mirrors if a signed CDN redirect link expired."""
        with self._refresh_lock:
            now = time.monotonic()
            if now - self._last_refresh_time < 5.0:
                return True
            candidates = [self.record.url] + [m for m in self.record.mirrors if m != self.record.url]
            last_err: Optional[Exception] = None
            for idx, candidate in enumerate(candidates):
                try:
                    label = "primary link" if idx == 0 else f"mirror #{idx}"
                    self.log(f"Link expired/failed — probing {label}...", "warn")
                    res = probe_url(
                        candidate,
                        user_agent=self.record.config.user_agent,
                        custom_headers=self.record.config.custom_headers,
                        timeout=self.record.config.connect_timeout,
                    )
                    self._validate_remote_identity(res)
                    self.record.final_url = res.final_url
                    if res.etag:
                        self.record.etag = res.etag
                    self.store.save_record(self.record)
                    self._last_refresh_time = time.monotonic()
                    self.log(f"Acquired active stream from {label}; resuming seamlessly.", "info")
                    return True
                except FatalDownloadError as exc:
                    last_err = exc
                    continue
                except Exception as exc:
                    last_err = exc
                    continue

            if last_err:
                self.log(f"URL refresh failed across {len(candidates)} source(s): {last_err}", "error")
            return False

    def _run_lifecycle(self) -> None:
        rec = self.record
        cfg = rec.config
        if rec.mode == "hls":
            self._run_hls_lifecycle()
            return
        try:
            if self._needs_probe:
                self._set_status(DownloadStatus.PROBING)
                self.log(f"Probing link: {rec.url}")
                probe = self._probe_with_retry()
                if probe is None:
                    return
                self._needs_probe = False
                rec.final_url = probe.final_url
                rec.total_bytes = probe.total_bytes
                rec.accept_ranges = probe.accept_ranges
                rec.etag = probe.etag
                rec.last_modified = probe.last_modified
                if not rec.filename or rec.filename == "download.bin":
                    rec.filename = probe.filename
                    rec.dest_path = _unique_dest_path(rec.dest_dir, rec.filename)
                    rec.part_path = rec.dest_path + ".part"

            already_bytes = rec.downloaded_bytes
            ok, err_msg, _ = check_storage_compatibility(rec.dest_dir, rec.total_bytes, already_bytes)
            if not ok:
                self.log(err_msg or "Storage check failed", "error")
                self._set_status(DownloadStatus.FAILED, err_msg)
                return

            seg_store = SegmentStore(rec.db_path)
            self._seg_store = seg_store

            if seg_store.has_segments():
                if not rec.accept_ranges:
                    self.log("Server does not support Range resume — restarting stream from 0", "warn")
                    seg_store.reset_unknown_size_stream()
                else:
                    # Lightweight remote identity check on resume when online
                    try:
                        resume_probe = probe_url(
                            rec.url,
                            user_agent=cfg.user_agent,
                            custom_headers=cfg.custom_headers,
                            timeout=min(8.0, cfg.connect_timeout),
                        )
                        self._validate_remote_identity(resume_probe)
                        rec.final_url = resume_probe.final_url
                    except FatalDownloadError:
                        raise
                    except Exception:
                        # Transient offline error: allow normal worker retry loop to recover
                        pass

                    seg_store.reset_active_to_pending()
                    resumed_bytes = seg_store.total_downloaded()
                    rec.downloaded_bytes = resumed_bytes
                    self.log(
                        f"Resuming with zero data loss from {resumed_bytes / (1024 * 1024):.2f} MB",
                        "info",
                    )
            else:
                segs = seg_store.initialize_segments(
                    total_bytes=rec.total_bytes,
                    accept_ranges=rec.accept_ranges,
                    segment_size_bytes=cfg.segment_size_bytes,
                )
                seg_store.set_meta(
                    {
                        "url": rec.url,
                        "final_url": rec.final_url,
                        "total_bytes": str(rec.total_bytes) if rec.total_bytes is not None else None,
                        "etag": rec.etag,
                        "last_modified": rec.last_modified,
                    }
                )
                self.log(
                    f"Initialized {len(segs)} segment(s) | Range support: {'Yes' if rec.accept_ranges else 'No (single stream)'}",
                    "info",
                )

            part_pool = PartFileHandlePool(rec.part_path, rec.total_bytes)
            self._part_pool = part_pool
            if rec.total_bytes and rec.total_bytes > 1024 * 1024:
                self.log(
                    f"Sparse random-access file ready ({'NTFS sparse enabled' if part_pool.is_sparse else 'standard'})",
                    "info",
                )

            adaptive = AdaptiveController(
                initial_connections=cfg.initial_connections,
                min_connections=cfg.min_connections,
                max_connections=cfg.max_connections,
                adaptive_enabled=cfg.adaptive_enabled,
                accept_ranges=rec.accept_ranges,
            )
            self._adaptive = adaptive
            self._set_status(DownloadStatus.DOWNLOADING)

            self._coordinate_workers(seg_store, part_pool, adaptive)

            rec.downloaded_bytes = seg_store.total_downloaded()
            part_pool.close_all()

            if self._cancel_requested:
                seg_store.close()
                self._cleanup_partial_files()
                self._set_status(DownloadStatus.CANCELLED)
                return

            if self._stop_event.is_set():
                seg_store.reset_active_to_pending()
                rec.downloaded_bytes = seg_store.total_downloaded()
                seg_store.close()
                self._set_status(DownloadStatus.PAUSED)
                self.log(
                    f"Paused safely at {rec.downloaded_bytes / (1024 * 1024):.2f} MB",
                    "info",
                )
                return

            if not seg_store.all_done():
                seg_store.close()
                if self.record.source_page_url and not self._stop_event.is_set() and not self._cancel_requested:
                    if self._download_via_ytdlp_fallback():
                        return
                return

            if rec.total_bytes is None:
                rec.total_bytes = rec.downloaded_bytes
            elif os.path.exists(rec.part_path) and os.path.getsize(rec.part_path) != rec.total_bytes:
                with open(rec.part_path, "r+b") as tf:
                    tf.truncate(rec.total_bytes)

            if rec.audio_url:
                if not self._download_and_mux_companion_audio():
                    seg_store.close()
                    return

            if rec.expected_sha256:
                self._set_status(DownloadStatus.VERIFYING)
                self.log("Verifying file integrity (SHA-256 streaming check)...")
                digest = compute_sha256_streaming(
                    rec.part_path,
                    progress_cb=self._on_verify_progress,
                    cancel_check=lambda: self._stop_event.is_set(),
                )
                if digest is None:
                    seg_store.close()
                    self._set_status(DownloadStatus.PAUSED)
                    return
                rec.actual_sha256 = digest
                if digest.lower() != rec.expected_sha256.strip().lower():
                    msg = f"SHA-256 mismatch! Expected {rec.expected_sha256}, got {digest}"
                    self.log(msg, "error")
                    seg_store.close()
                    self._set_status(DownloadStatus.FAILED, msg)
                    return
                self.log("SHA-256 checksum verified successfully!", "info")

            os.replace(rec.part_path, rec.dest_path)
            seg_store.close()
            self._remove_db_sidecars(rec.db_path)

            rec.completed_at = time.time()
            self._set_status(DownloadStatus.COMPLETED)
            self.log(
                f"Download completed cleanly: {rec.dest_path} ({rec.downloaded_bytes / (1024**3):.3f} GB)",
                "info",
            )
            if self._on_complete_cb:
                self._on_complete_cb(self)

        except FatalDownloadError as exc:
            self.log(f"Fatal error: {exc}", "error")
            self._set_status(DownloadStatus.FAILED, str(exc))
        except Exception as exc:
            self.log(f"Unexpected error: {exc}", "error")
            self._set_status(DownloadStatus.FAILED, str(exc))
        finally:
            if self._part_pool is not None:
                self._part_pool.close_all()
            if self._seg_store is not None:
                self._seg_store.close()
            with self._lock:
                self._workers.clear()
                self._worker_threads.clear()
            if self._on_finish_cb:
                self._on_finish_cb(self)

    def _download_and_mux_companion_audio(self) -> bool:
        """Download paired audio stream (for 1080p/2K/4K split streams) and mux with FFmpeg."""
        rec = self.record
        cfg = rec.config
        if not rec.audio_url:
            return True
        ffmpeg_bin = find_ffmpeg()
        if not ffmpeg_bin:
            self.log("FFmpeg not detected on PC — auto-provisioning portable FFmpeg for HD audio merge...", "info")
            ffmpeg_bin = ensure_ffmpeg(auto_install=True)
        if not ffmpeg_bin:
            self.log("FFmpeg unavailable — saving primary video stream without audio merge", "warn")
            return True

        self._set_status(DownloadStatus.MERGING)
        audio_part = rec.part_path + ".audio"
        self.log("Downloading companion high-bitrate audio track...", "info")

        retries = 0
        while not self._stop_event.is_set():
            existing_audio = os.path.getsize(audio_part) if os.path.exists(audio_part) else 0
            stream = None
            try:
                stream = open_range_stream(
                    url=rec.audio_url,
                    start_byte=existing_audio,
                    end_byte=None,
                    user_agent=cfg.user_agent,
                    custom_headers=cfg.custom_headers,
                    timeout=cfg.read_timeout,
                )
                mode = "ab" if (existing_audio > 0 and stream.status_code == 206) else "wb"
                with open(audio_part, mode) as af:
                    while not self._stop_event.is_set():
                        chunk = stream.read(cfg.read_buffer_bytes)
                        if not chunk:
                            break
                        self._limiter.acquire(len(chunk), self._stop_event)
                        if self._stop_event.is_set():
                            break
                        af.write(chunk)
                if self._stop_event.is_set():
                    self._set_status(DownloadStatus.PAUSED)
                    return False
                break
            except Exception as exc:
                retries += 1
                if retries > 8:
                    msg = f"Failed to download companion audio track: {exc}"
                    self.log(msg, "error")
                    self._set_status(DownloadStatus.FAILED, msg)
                    return False
                self._stop_event.wait(min(12.0, 1.5 * retries))
            finally:
                if stream is not None:
                    stream.close(force_close=True)

        if self._stop_event.is_set():
            self._set_status(DownloadStatus.PAUSED)
            return False

        import subprocess

        self.log("Merging HD video and audio tracks with FFmpeg...", "info")
        tmp_mux = rec.part_path + ".mux.mp4"
        no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        cmd_copy = [
            ffmpeg_bin, "-y",
            "-i", rec.part_path,
            "-i", audio_part,
            "-c", "copy",
            "-movflags", "+faststart",
            tmp_mux,
        ]
        proc = subprocess.run(cmd_copy, capture_output=True, timeout=600, creationflags=no_win)
        if proc.returncode != 0 or not os.path.exists(tmp_mux) or os.path.getsize(tmp_mux) == 0:
            # Fallback: transcode audio to AAC if source audio was Opus/Vorbis and container is MP4
            cmd_aac = [
                ffmpeg_bin, "-y",
                "-i", rec.part_path,
                "-i", audio_part,
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                tmp_mux,
            ]
            proc = subprocess.run(cmd_aac, capture_output=True, timeout=600, creationflags=no_win)

        if proc.returncode == 0 and os.path.exists(tmp_mux) and os.path.getsize(tmp_mux) > 0:
            os.replace(tmp_mux, rec.part_path)
            final_sz = os.path.getsize(rec.part_path)
            rec.total_bytes = final_sz
            rec.downloaded_bytes = final_sz
            self.log(f"FFmpeg HD video+audio merge complete ({final_sz / (1024 * 1024):.2f} MB)", "info")
        else:
            if os.path.exists(tmp_mux):
                try:
                    os.remove(tmp_mux)
                except OSError:
                    pass
            self.log("FFmpeg merge returned non-zero; keeping primary video stream", "warn")

        if os.path.exists(audio_part):
            try:
                os.remove(audio_part)
            except OSError:
                pass
        return True

    def _download_via_ytdlp_fallback(self) -> bool:
        """Fallback to yt-dlp's native chunked/fragment engine if a social/YouTube CDN stream rejected raw Range requests."""
        rec = self.record
        if not rec.source_page_url:
            return False
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            return False

        self.log("Stream requires native chunked session — switching to yt-dlp engine...", "info")
        self._set_status(DownloadStatus.DOWNLOADING, None)
        ffmpeg_path = find_ffmpeg()
        last_bytes = [0]

        def _hook(d: dict) -> None:
            if self._stop_event.is_set():
                raise FatalDownloadError("Paused by user")
            st = d.get("status")
            if st == "downloading":
                dl_b = int(d.get("downloaded_bytes") or 0)
                tot_b = d.get("total_bytes") or d.get("total_bytes_estimate")
                if tot_b:
                    rec.total_bytes = int(tot_b)
                delta = max(0, dl_b - last_bytes[0])
                last_bytes[0] = dl_b
                rec.downloaded_bytes = dl_b
                if self._adaptive and delta > 0:
                    self._adaptive.record_bytes(delta)
            elif st == "finished":
                self._set_status(DownloadStatus.MERGING, None)

        out_tmpl = os.path.splitext(rec.dest_path)[0] + ".%(ext)s"
        opts: Dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "outtmpl": out_tmpl,
            "concurrent_fragment_downloads": max(1, min(8, rec.config.max_connections)),
            "progress_hooks": [_hook],
            "merge_output_format": "mp4",
        }
        if ffmpeg_path:
            opts["ffmpeg_location"] = ffmpeg_path
        if rec.config.custom_headers:
            opts["http_headers"] = dict(rec.config.custom_headers)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([rec.source_page_url])
            # Locate produced file
            stem = os.path.splitext(rec.dest_path)[0]
            produced = None
            for ext_cand in (".mp4", ".mkv", ".webm", ".m4a"):
                if os.path.exists(stem + ext_cand):
                    produced = stem + ext_cand
                    break
            if produced and os.path.exists(produced):
                rec.dest_path = produced
                rec.filename = os.path.basename(produced)
                final_sz = os.path.getsize(produced)
                rec.total_bytes = final_sz
                rec.downloaded_bytes = final_sz
                self._cleanup_partial_files()
                rec.completed_at = time.time()
                self._set_status(DownloadStatus.COMPLETED, None)
                self.log(f"Video download completed via yt-dlp engine: {rec.dest_path}", "info")
                if self._on_complete_cb:
                    self._on_complete_cb(self)
                return True
        except Exception as exc:
            if self._stop_event.is_set():
                self._set_status(DownloadStatus.PAUSED, None)
            else:
                self._set_status(DownloadStatus.FAILED, str(exc))
        return False

    # --- Parallel Adaptive HLS (.m3u8) Engine ---

    def _run_hls_lifecycle(self) -> None:
        rec = self.record
        cfg = rec.config
        assemble_lock = threading.Lock()
        try:
            ok, err_msg, _ = check_storage_compatibility(rec.dest_dir, None, rec.downloaded_bytes)
            if not ok:
                self.log(err_msg or "Storage check failed", "error")
                self._set_status(DownloadStatus.FAILED, err_msg)
                return

            seg_store = SegmentStore(rec.db_path)
            self._seg_store = seg_store

            if seg_store.has_hls_segments():
                seg_store.reset_active_hls_to_pending()
                total_cnt, done_cnt, bytes_so_far = seg_store.hls_stats()
                rec.downloaded_bytes = bytes_so_far
                self.log(
                    f"Resuming HLS stream cleanly: {done_cnt}/{total_cnt} segments saved ({bytes_so_far / (1024 * 1024):.2f} MB)",
                    "info",
                )
            else:
                self._set_status(DownloadStatus.PROBING)
                self.log(f"Resolving HLS playlist: {rec.final_url}")
                hls_info = resolve_hls_media_playlist(
                    rec.final_url,
                    user_agent=cfg.user_agent,
                    custom_headers=cfg.custom_headers,
                    timeout=cfg.connect_timeout,
                )
                seg_store.initialize_hls_segments(hls_info.segment_urls)
                self.log(
                    f"Initialized {len(hls_info.segment_urls)} HLS media segment(s) (~{hls_info.total_duration:.0f}s)",
                    "info",
                )

            os.makedirs(os.path.dirname(os.path.abspath(rec.part_path)), exist_ok=True)
            if not os.path.exists(rec.part_path):
                with open(rec.part_path, "wb"):
                    pass

            adaptive = AdaptiveController(
                initial_connections=cfg.initial_connections,
                min_connections=cfg.min_connections,
                max_connections=cfg.max_connections,
                adaptive_enabled=cfg.adaptive_enabled,
                accept_ranges=True,
            )
            self._adaptive = adaptive
            self._set_status(DownloadStatus.DOWNLOADING)

            self._coordinate_hls_workers(seg_store, adaptive, assemble_lock)

            # Final contiguous assembly flush
            self._assemble_ready_hls_segments(seg_store, assemble_lock)
            _, _, bytes_so_far = seg_store.hls_stats()
            rec.downloaded_bytes = bytes_so_far

            if self._cancel_requested:
                seg_store.close()
                self._cleanup_partial_files()
                self._set_status(DownloadStatus.CANCELLED)
                return

            if self._stop_event.is_set():
                seg_store.reset_active_hls_to_pending()
                seg_store.close()
                self._set_status(DownloadStatus.PAUSED)
                self.log(f"Paused HLS stream safely at {bytes_so_far / (1024 * 1024):.2f} MB", "info")
                return

            if not seg_store.all_hls_done():
                seg_store.close()
                return

            final_size = os.path.getsize(rec.part_path) if os.path.exists(rec.part_path) else bytes_so_far
            rec.total_bytes = final_size
            rec.downloaded_bytes = final_size

            # Remux MPEG-TS -> MP4 if ffmpeg is available and destination is .mp4
            ffmpeg_bin = find_ffmpeg() or (ensure_ffmpeg(auto_install=True) if rec.dest_path.lower().endswith(".mp4") else None)
            if ffmpeg_bin and rec.dest_path.lower().endswith(".mp4"):
                import subprocess

                tmp_mux = rec.part_path + ".mux.mp4"
                proc = subprocess.run(
                    [ffmpeg_bin, "-y", "-i", rec.part_path, "-c", "copy", "-movflags", "+faststart", tmp_mux],
                    capture_output=True,
                    timeout=300,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if proc.returncode == 0 and os.path.exists(tmp_mux) and os.path.getsize(tmp_mux) > 0:
                    os.replace(tmp_mux, rec.dest_path)
                    try:
                        os.remove(rec.part_path)
                    except OSError:
                        pass
                else:
                    if os.path.exists(tmp_mux):
                        os.remove(tmp_mux)
                    os.replace(rec.part_path, rec.dest_path)
            else:
                os.replace(rec.part_path, rec.dest_path)

            seg_store.close()
            self._remove_db_sidecars(rec.db_path)
            rec.completed_at = time.time()
            self._set_status(DownloadStatus.COMPLETED)
            self.log(f"HLS video completed: {rec.dest_path} ({final_size / (1024 * 1024):.2f} MB)", "info")
            if self._on_complete_cb:
                self._on_complete_cb(self)

        except FatalDownloadError as exc:
            self.log(f"HLS fatal error: {exc}", "error")
            self._set_status(DownloadStatus.FAILED, str(exc))
        except Exception as exc:
            self.log(f"HLS unexpected error: {exc}", "error")
            self._set_status(DownloadStatus.FAILED, str(exc))
        finally:
            if self._seg_store is not None:
                self._seg_store.close()
            with self._lock:
                self._workers.clear()
                self._worker_threads.clear()
            if self._on_finish_cb:
                self._on_finish_cb(self)

    def _assemble_ready_hls_segments(self, seg_store: SegmentStore, assemble_lock: threading.Lock) -> None:
        with assemble_lock:
            ready_indices = seg_store.get_ready_unassembled_hls()
            if not ready_indices:
                return
            with open(self.record.part_path, "ab") as out_f:
                for idx in ready_indices:
                    seg_file = f"{self.record.db_path}.seg.{idx}"
                    if not os.path.exists(seg_file):
                        # Re-queue missing temp segment safely
                        seg_store.mark_hls_segment_pending(idx)
                        break
                    with open(seg_file, "rb") as in_f:
                        shutil.copyfileobj(in_f, out_f, 1024 * 1024)
                    out_f.flush()
                    seg_store.mark_hls_assembled(idx)
                    try:
                        os.remove(seg_file)
                    except OSError:
                        pass

    def _coordinate_hls_workers(
        self,
        seg_store: SegmentStore,
        adaptive: AdaptiveController,
        assemble_lock: threading.Lock,
    ) -> None:
        fatal_error: List[str] = []
        last_reason = adaptive.reason
        last_db_save = time.monotonic()

        def spawn_worker(wid: int) -> None:
            t = threading.Thread(
                target=self._hls_worker_loop,
                args=(wid, seg_store, adaptive, assemble_lock, fatal_error),
                name=f"happydl-hls-w{wid}-{self.record.id[:6]}",
                daemon=True,
            )
            with self._lock:
                self._worker_threads[wid] = t
                self._workers[wid] = WorkerSnapshot(
                    worker_id=wid,
                    segment_id=None,
                    speed_bps=0.0,
                    bytes_session=0,
                    state="starting",
                )
            t.start()

        for _ in range(adaptive.target_connections):
            wid = self._next_worker_id
            self._next_worker_id += 1
            spawn_worker(wid)

        while not self._stop_event.is_set():
            if fatal_error:
                self._stop_event.set()
                self._set_status(DownloadStatus.FAILED, fatal_error[0])
                break

            with self._lock:
                dead_ids = [wid for wid, th in self._worker_threads.items() if not th.is_alive()]
                for wid in dead_ids:
                    self._worker_threads.pop(wid, None)
                    self._workers.pop(wid, None)
                    self._worker_errors.pop(wid, None)
                active_count = len(self._worker_threads)
                all_errored = active_count > 0 and all(self._worker_errors.get(w, False) for w in self._worker_threads)

            total_cnt, done_cnt, total_bytes_w = seg_store.hls_stats()
            has_work = done_cnt < total_cnt
            if active_count == 0 and not has_work:
                break

            if all_errored and self.record.status != DownloadStatus.WAITING_NETWORK:
                self._set_status(DownloadStatus.WAITING_NETWORK, "Waiting for network recovery...")
            elif not all_errored and self.record.status == DownloadStatus.WAITING_NETWORK:
                self._set_status(DownloadStatus.DOWNLOADING, None)
                self.log("Network connectivity restored — continuing HLS stream", "info")

            target = adaptive.tick(active_workers=active_count, remaining_segments_exist=has_work)
            if adaptive.reason != last_reason:
                last_reason = adaptive.reason
                self.log(f"Adaptive engine: {last_reason}", "info")

            if active_count < target and has_work:
                for _ in range(target - active_count):
                    wid = self._next_worker_id
                    self._next_worker_id += 1
                    spawn_worker(wid)

            now = time.monotonic()
            if now - last_db_save >= 2.0:
                self.record.downloaded_bytes = total_bytes_w
                self.store.save_record(self.record)
                last_db_save = now

            self._stop_event.wait(0.5)

        with self._lock:
            threads = list(self._worker_threads.values())
        for th in threads:
            th.join(timeout=10.0)

    def _hls_worker_loop(
        self,
        wid: int,
        seg_store: SegmentStore,
        adaptive: AdaptiveController,
        assemble_lock: threading.Lock,
        fatal_error: List[str],
    ) -> None:
        cfg = self.record.config
        http_client = KeepAliveClient()
        try:
            while not self._stop_event.is_set() and not fatal_error:
                with self._lock:
                    active_cnt = len(self._worker_threads)
                if active_cnt > adaptive.target_connections and wid > adaptive.target_connections:
                    return

                claimed = seg_store.claim_next_hls_segment()
                if claimed is None:
                    return
                seg_idx, seg_url = claimed
                seg_file = f"{self.record.db_path}.seg.{seg_idx}"
                retries = 0
                done_seg = False

                while not self._stop_event.is_set() and not fatal_error:
                    with self._lock:
                        snap = self._workers.get(wid)
                        if snap:
                            snap.segment_id = seg_idx
                            snap.state = "downloading"
                            snap.retries = retries

                    stream = None
                    force_close = False
                    bytes_seg = 0
                    try:
                        stream = open_range_stream(
                            url=seg_url,
                            start_byte=0,
                            end_byte=None,
                            user_agent=cfg.user_agent,
                            custom_headers=cfg.custom_headers,
                            timeout=cfg.read_timeout,
                            client=http_client,
                        )
                        win_start = time.monotonic()
                        win_bytes = 0
                        with open(seg_file, "wb") as out_seg:
                            while not self._stop_event.is_set():
                                chunk = stream.read(cfg.read_buffer_bytes)
                                if not chunk:
                                    break
                                self._limiter.acquire(len(chunk), self._stop_event)
                                if self._stop_event.is_set():
                                    force_close = True
                                    break
                                out_seg.write(chunk)
                                n = len(chunk)
                                bytes_seg += n
                                win_bytes += n
                                adaptive.record_bytes(n)

                                now = time.monotonic()
                                dt = now - win_start
                                if dt >= 0.4:
                                    with self._lock:
                                        snap = self._workers.get(wid)
                                        if snap:
                                            snap.speed_bps = win_bytes / dt
                                            snap.bytes_session += win_bytes
                                    win_start = now
                                    win_bytes = 0

                        if self._stop_event.is_set():
                            force_close = True
                            if os.path.exists(seg_file):
                                try:
                                    os.remove(seg_file)
                                except OSError:
                                    pass
                            seg_store.mark_hls_segment_pending(seg_idx)
                            return

                        with self._lock:
                            self._worker_errors[wid] = False
                        seg_store.mark_hls_segment_done(seg_idx, bytes_seg)
                        self._assemble_ready_hls_segments(seg_store, assemble_lock)
                        done_seg = True
                        break

                    except FatalDownloadError as exc:
                        force_close = True
                        seg_store.mark_hls_segment_pending(seg_idx)
                        fatal_error.append(str(exc))
                        return
                    except ThrottleError as exc:
                        force_close = True
                        adaptive.report_throttle()
                        retries += 1
                        with self._lock:
                            self._worker_errors[wid] = True
                            snap = self._workers.get(wid)
                            if snap:
                                snap.state = "throttled"
                                snap.last_error = str(exc)
                        self._stop_event.wait(exc.retry_after or min(25.0, 2.0 * retries))
                    except (
                        TransientNetworkError,
                        LinkExpiredError,
                        http.client.HTTPException,
                        socket.timeout,
                        TimeoutError,
                        ConnectionError,
                        OSError,
                        ssl.SSLError,
                    ) as exc:
                        force_close = True
                        adaptive.report_error()
                        retries += 1
                        with self._lock:
                            self._worker_errors[wid] = True
                            snap = self._workers.get(wid)
                            if snap:
                                snap.state = "retrying"
                                snap.retries = retries
                                snap.last_error = str(exc)
                        if retries > cfg.max_retries_per_segment:
                            seg_store.mark_hls_segment_pending(seg_idx)
                            fatal_error.append(f"HLS segment {seg_idx} failed after {retries} retries: {exc}")
                            return
                        self._stop_event.wait(min(20.0, (1.5 ** min(retries, 6)) + random.uniform(0.2, 1.0)))
                    finally:
                        if stream is not None:
                            stream.close(force_close=force_close)

                if not done_seg:
                    seg_store.mark_hls_segment_pending(seg_idx)
                    return
        finally:
            with self._lock:
                self._keepalive_reuses += http_client.reused_count
            http_client.close()

    def _on_verify_progress(self, done: int, total: int) -> None:
        self._verify_progress = (done / total) if total > 0 else 1.0

    def _probe_with_retry(self) -> Optional[ProbeResult]:
        rec = self.record
        attempt = 0
        while not self._stop_event.is_set():
            try:
                return probe_url(
                    rec.url,
                    user_agent=rec.config.user_agent,
                    custom_headers=rec.config.custom_headers,
                    timeout=rec.config.connect_timeout,
                )
            except (TransientNetworkError, ThrottleError) as exc:
                attempt += 1
                self._set_status(DownloadStatus.WAITING_NETWORK, str(exc))
                delay = min(30.0, (2.0 ** min(attempt, 5)) + random.uniform(0.2, 1.0))
                self.log(f"Probe retry #{attempt} in {delay:.1f}s ({exc})", "warn")
                self._stop_event.wait(delay)
            except FatalDownloadError as exc:
                self.log(f"Probe failed: {exc}", "error")
                self._set_status(DownloadStatus.FAILED, str(exc))
                return None
        self._set_status(DownloadStatus.PAUSED)
        return None

    def _coordinate_workers(
        self,
        seg_store: SegmentStore,
        part_pool: PartFileHandlePool,
        adaptive: AdaptiveController,
    ) -> None:
        fatal_error: List[str] = []
        last_reason = adaptive.reason
        last_db_save = time.monotonic()

        def spawn_worker(wid: int) -> None:
            t = threading.Thread(
                target=self._worker_loop,
                args=(wid, seg_store, part_pool, adaptive, fatal_error),
                name=f"happydl-w{wid}-{self.record.id[:6]}",
                daemon=True,
            )
            with self._lock:
                self._worker_threads[wid] = t
                self._workers[wid] = WorkerSnapshot(
                    worker_id=wid,
                    segment_id=None,
                    speed_bps=0.0,
                    bytes_session=0,
                    state="starting",
                )
            t.start()

        initial_target = adaptive.target_connections
        for _ in range(initial_target):
            wid = self._next_worker_id
            self._next_worker_id += 1
            spawn_worker(wid)

        while not self._stop_event.is_set():
            if fatal_error:
                self._stop_event.set()
                self._set_status(DownloadStatus.FAILED, fatal_error[0])
                break

            with self._lock:
                dead_ids = [wid for wid, th in self._worker_threads.items() if not th.is_alive()]
                for wid in dead_ids:
                    self._worker_threads.pop(wid, None)
                    self._workers.pop(wid, None)
                    self._worker_errors.pop(wid, None)
                active_count = len(self._worker_threads)
                all_errored = active_count > 0 and all(self._worker_errors.get(w, False) for w in self._worker_threads)

            if active_count == 0 and seg_store.all_done():
                break

            if all_errored and self.record.status != DownloadStatus.WAITING_NETWORK:
                self._set_status(DownloadStatus.WAITING_NETWORK, "Waiting for network recovery...")
            elif not all_errored and self.record.status == DownloadStatus.WAITING_NETWORK:
                self._set_status(DownloadStatus.DOWNLOADING, None)
                self.log("Network connectivity restored — continuing download", "info")

            segs = seg_store.list_segments()
            has_work = any(s.status != SegmentStatus.DONE for s in segs)
            if active_count == 0 and not has_work:
                break

            target = adaptive.tick(active_workers=active_count, remaining_segments_exist=has_work)
            if adaptive.reason != last_reason:
                last_reason = adaptive.reason
                self.log(f"Adaptive engine: {last_reason}", "info")

            if active_count < target and has_work:
                needed = target - active_count
                for _ in range(needed):
                    wid = self._next_worker_id
                    self._next_worker_id += 1
                    spawn_worker(wid)

            now = time.monotonic()
            if now - last_db_save >= 2.0:
                self.record.downloaded_bytes = seg_store.total_downloaded()
                self.store.save_record(self.record)
                last_db_save = now

            self._stop_event.wait(0.5)

        with self._lock:
            threads = list(self._worker_threads.values())
        for th in threads:
            th.join(timeout=10.0)

    def _worker_loop(
        self,
        wid: int,
        seg_store: SegmentStore,
        part_pool: PartFileHandlePool,
        adaptive: AdaptiveController,
        fatal_error: List[str],
    ) -> None:
        http_client = KeepAliveClient()
        try:
            while not self._stop_event.is_set() and not fatal_error:
                with self._lock:
                    active_cnt = len(self._worker_threads)
                if active_cnt > adaptive.target_connections and wid > adaptive.target_connections:
                    return

                seg = seg_store.claim_next_segment()
                if seg is None:
                    return

                success = self._download_segment(
                    wid, seg, seg_store, part_pool, adaptive, fatal_error, http_client
                )
                if not success:
                    return
        finally:
            with self._lock:
                self._keepalive_reuses += http_client.reused_count
            http_client.close()

    def _download_segment(
        self,
        wid: int,
        seg: Segment,
        seg_store: SegmentStore,
        part_pool: PartFileHandlePool,
        adaptive: AdaptiveController,
        fatal_error: List[str],
        http_client: KeepAliveClient,
    ) -> bool:
        cfg = self.record.config
        retries = 0
        bytes_since_checkpoint = 0
        last_checkpoint_time = time.monotonic()

        while not self._stop_event.is_set() and not fatal_error:
            current_end = seg_store.get_segment_end(seg.segment_id)
            if current_end >= 0:
                seg.end_byte = current_end
            if seg.is_complete:
                seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes, SegmentStatus.DONE)
                return True

            start_offset = seg.current_offset
            req_end = seg.end_byte if seg.end_byte >= 0 else None

            with self._lock:
                snap = self._workers.get(wid)
                if snap:
                    snap.segment_id = seg.segment_id
                    snap.state = "connecting"
                    snap.retries = retries

            stream = None
            force_close_stream = False
            try:
                stream = open_range_stream(
                    url=self.record.final_url,
                    start_byte=start_offset,
                    end_byte=req_end,
                    user_agent=cfg.user_agent,
                    custom_headers=cfg.custom_headers,
                    timeout=cfg.read_timeout,
                    client=http_client,
                )
                with self._lock:
                    self._worker_errors[wid] = False
                    snap = self._workers.get(wid)
                    if snap:
                        snap.state = "downloading"
                        snap.last_error = None

                win_start = time.monotonic()
                win_bytes = 0
                low_speed_since: Optional[float] = None

                while not self._stop_event.is_set():
                    chunk = stream.read(cfg.read_buffer_bytes)
                    if not chunk:
                        break

                    if seg.end_byte >= 0:
                        if bytes_since_checkpoint >= cfg.checkpoint_interval_bytes // 2 or (
                            seg.current_offset + len(chunk) > seg.end_byte - cfg.checkpoint_interval_bytes
                        ):
                            latest_end = seg_store.get_segment_end(seg.segment_id)
                            if latest_end >= 0:
                                if latest_end < seg.end_byte:
                                    # Tail was stolen by another worker; close socket cleanly at boundary
                                    force_close_stream = True
                                seg.end_byte = latest_end

                        remaining = seg.remaining_bytes or 0
                        if remaining <= 0:
                            force_close_stream = True
                            break
                        if len(chunk) > remaining:
                            chunk = chunk[:remaining]
                            force_close_stream = True

                    self._limiter.acquire(len(chunk), self._stop_event)
                    if self._stop_event.is_set():
                        force_close_stream = True
                        break

                    part_pool.write_at(seg.current_offset, chunk)
                    n = len(chunk)
                    seg.downloaded_bytes += n
                    bytes_since_checkpoint += n
                    win_bytes += n
                    adaptive.record_bytes(n)

                    now = time.monotonic()
                    dt = now - win_start
                    if dt >= 0.5:
                        speed = win_bytes / dt
                        with self._lock:
                            snap = self._workers.get(wid)
                            if snap:
                                snap.speed_bps = speed
                                snap.bytes_session += win_bytes
                        if speed < cfg.stall_min_bps:
                            if low_speed_since is None:
                                low_speed_since = now
                            elif now - low_speed_since >= cfg.stall_timeout:
                                raise TransientNetworkError(
                                    f"Connection stalled (< {cfg.stall_min_bps:.0f} B/s for {cfg.stall_timeout:.0f}s)"
                                )
                        else:
                            low_speed_since = None
                        win_start = now
                        win_bytes = 0

                    if (
                        bytes_since_checkpoint >= cfg.checkpoint_interval_bytes
                        or now - last_checkpoint_time >= 1.5
                    ):
                        part_pool.flush_thread_handle()
                        seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes)
                        bytes_since_checkpoint = 0
                        last_checkpoint_time = now

                    if seg.end_byte >= 0 and seg.is_complete:
                        break

                part_pool.flush_thread_handle()
                if self._stop_event.is_set():
                    force_close_stream = True
                    seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes, SegmentStatus.PENDING)
                    return False

                if seg.end_byte >= 0:
                    latest_end = seg_store.get_segment_end(seg.segment_id)
                    if latest_end >= 0:
                        seg.end_byte = latest_end
                    if seg.is_complete:
                        seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes, SegmentStatus.DONE)
                        return True
                    force_close_stream = True
                    raise TransientNetworkError("Server closed stream prematurely before segment end")
                else:
                    seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes, SegmentStatus.DONE)
                    return True

            except FatalDownloadError as exc:
                force_close_stream = True
                # If mirrors exist and primary gave 404/fatal, attempt mirror failover before failing
                if self.record.mirrors and self._refresh_final_url():
                    continue
                part_pool.flush_thread_handle()
                seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes, SegmentStatus.PENDING)
                fatal_error.append(str(exc))
                return False

            except LinkExpiredError as exc:
                force_close_stream = True
                part_pool.flush_thread_handle()
                seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes)
                if not self._refresh_final_url():
                    retries += 1
                    adaptive.report_error()
                    if retries > cfg.max_retries_per_segment:
                        fatal_error.append(f"Link refresh failed after {retries} attempts: {exc}")
                        return False
                    self._stop_event.wait(min(20.0, 2.0 * retries))
                continue

            except ThrottleError as exc:
                force_close_stream = True
                part_pool.flush_thread_handle()
                seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes)
                adaptive.report_throttle()
                retries += 1
                delay = exc.retry_after or min(30.0, (1.5 ** min(retries, 6)) + random.uniform(0.3, 1.5))
                with self._lock:
                    self._worker_errors[wid] = True
                    snap = self._workers.get(wid)
                    if snap:
                        snap.state = "throttled"
                        snap.speed_bps = 0.0
                        snap.last_error = str(exc)
                self._stop_event.wait(delay)

            except (
                TransientNetworkError,
                http.client.HTTPException,
                socket.timeout,
                TimeoutError,
                ConnectionError,
                OSError,
                ssl.SSLError,
            ) as exc:
                force_close_stream = True
                part_pool.flush_thread_handle()
                seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes)
                adaptive.report_error()
                retries += 1
                with self._lock:
                    self._worker_errors[wid] = True
                    snap = self._workers.get(wid)
                    if snap:
                        snap.state = "retrying"
                        snap.speed_bps = 0.0
                        snap.retries = retries
                        snap.last_error = str(exc)

                delay = min(25.0, (1.6 ** min(retries, 7)) + random.uniform(0.2, 1.2))
                self._stop_event.wait(delay)

            finally:
                if stream is not None:
                    stream.close(force_close=force_close_stream)

        part_pool.flush_thread_handle()
        seg_store.checkpoint_segment(seg.segment_id, seg.downloaded_bytes, SegmentStatus.PENDING)
        return False

    def _remove_db_sidecars(self, db_path: str) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _cleanup_partial_files(self) -> None:
        for cand in (
            self.record.part_path,
            self.record.part_path + ".audio",
            self.record.part_path + ".mux.mp4",
        ):
            if os.path.exists(cand):
                try:
                    os.remove(cand)
                except OSError:
                    pass
        # Clean any buffered HLS temp segments `.seg.<idx>`
        db_dir = os.path.dirname(os.path.abspath(self.record.db_path))
        db_base = os.path.basename(self.record.db_path) + ".seg."
        if os.path.isdir(db_dir):
            try:
                for entry in os.scandir(db_dir):
                    if entry.name.startswith(db_base):
                        try:
                            os.remove(entry.path)
                        except OSError:
                            pass
            except OSError:
                pass
        self._remove_db_sidecars(self.record.db_path)

    def _compute_segment_map(self, buckets: int = 80) -> List[int]:
        now = time.monotonic()
        if self._seg_store is None:
            if self.record.status == DownloadStatus.COMPLETED:
                return [2] * buckets
            return self._segment_summary_cache or [0] * buckets

        if now - self._last_summary_ts < 0.7 and self._segment_summary_cache:
            return self._segment_summary_cache

        if self.record.mode == "hls":
            try:
                statuses = self._seg_store.hls_status_list()
            except Exception:
                return self._segment_summary_cache or [0] * buckets
            if not statuses:
                return [0] * buckets
            res: List[int] = []
            n = len(statuses)
            for b in range(buckets):
                start_i = (b * n) // buckets
                end_i = max(start_i + 1, ((b + 1) * n) // buckets)
                sl = statuses[start_i:end_i]
                if any(s == "active" for s in sl):
                    res.append(1)
                elif all(s == "done" for s in sl):
                    res.append(2)
                elif any(s == "done" for s in sl):
                    res.append(1)
                else:
                    res.append(0)
            self._segment_summary_cache = res
            self._last_summary_ts = now
            return res

        try:
            segs = self._seg_store.list_segments()
        except Exception:
            return self._segment_summary_cache or [0] * buckets

        if not segs:
            return [0] * buckets

        res: List[int] = []
        n = len(segs)
        for b in range(buckets):
            start_i = (b * n) // buckets
            end_i = max(start_i + 1, ((b + 1) * n) // buckets)
            slice_segs = segs[start_i:end_i]
            if any(s.status == SegmentStatus.ACTIVE for s in slice_segs):
                res.append(1)
            elif all(s.status == SegmentStatus.DONE for s in slice_segs):
                res.append(2)
            elif any(s.status == SegmentStatus.DONE or s.downloaded_bytes > 0 for s in slice_segs):
                res.append(1)
            else:
                res.append(0)
        self._segment_summary_cache = res
        self._last_summary_ts = now
        return res

    def to_dict(self) -> Dict[str, object]:
        rec = self.record
        downloaded = rec.downloaded_bytes
        hls_total_segs = 0
        hls_done_segs = 0
        if self._seg_store is not None and self.is_running():
            try:
                if rec.mode == "hls":
                    hls_total_segs, hls_done_segs, downloaded = self._seg_store.hls_stats()
                else:
                    downloaded = self._seg_store.total_downloaded()
                rec.downloaded_bytes = downloaded
            except Exception:
                pass

        adaptive_snap = (
            self._adaptive.snapshot()
            if self._adaptive is not None
            else {
                "target_connections": rec.config.initial_connections if (rec.accept_ranges or rec.mode == "hls") else 1,
                "current_bps": 0.0,
                "ewma_bps": 0.0,
                "peak_bps": 0.0,
                "reason": "Queued (waiting for slot)" if rec.status == DownloadStatus.QUEUED else "Idle",
                "adaptive_enabled": rec.config.adaptive_enabled,
                "history": [],
            }
        )

        speed_bps = float(adaptive_snap["current_bps"]) if self.is_running() else 0.0
        eta_seconds: Optional[float] = None
        if rec.mode == "hls" and hls_total_segs > 0 and hls_done_segs > 0 and speed_bps > 100.0:
            avg_seg_bytes = downloaded / float(hls_done_segs)
            est_total = avg_seg_bytes * hls_total_segs
            eta_seconds = max(0.0, est_total - downloaded) / speed_bps
        elif rec.total_bytes and rec.total_bytes > 0 and speed_bps > 100.0:
            rem = max(0, rec.total_bytes - downloaded)
            eta_seconds = rem / speed_bps

        progress = 0.0
        if rec.status == DownloadStatus.COMPLETED:
            progress = 100.0
        elif rec.mode == "hls" and hls_total_segs > 0:
            progress = min(99.9, (hls_done_segs / hls_total_segs) * 100.0)
        elif rec.total_bytes and rec.total_bytes > 0:
            progress = min(100.0, (downloaded / rec.total_bytes) * 100.0)

        with self._lock:
            workers = [
                {
                    "worker_id": w.worker_id,
                    "segment_id": w.segment_id,
                    "speed_bps": w.speed_bps,
                    "bytes_session": w.bytes_session,
                    "state": w.state,
                    "retries": w.retries,
                    "last_error": w.last_error,
                }
                for w in sorted(self._workers.values(), key=lambda x: x.worker_id)
            ]
            logs = list(self._logs)[-25:]

        elapsed_sec = max(0.0, (rec.completed_at or time.time()) - rec.created_at)
        avg_speed_bps = (downloaded / elapsed_sec) if elapsed_sec >= 0.5 and downloaded > 0 else 0.0

        return {
            "id": rec.id,
            "url": rec.url,
            "final_url": rec.final_url,
            "mirrors": list(rec.mirrors),
            "filename": rec.filename,
            "dest_dir": rec.dest_dir,
            "dest_path": rec.dest_path,
            "total_bytes": rec.total_bytes,
            "downloaded_bytes": downloaded,
            "progress": round(progress, 2),
            "accept_ranges": rec.accept_ranges,
            "mode": rec.mode,
            "source_page_url": rec.source_page_url,
            "audio_url": rec.audio_url,
            "status": rec.status.value,
            "error_message": rec.error_message,
            "expected_sha256": rec.expected_sha256,
            "actual_sha256": rec.actual_sha256,
            "verify_progress": round(self._verify_progress * 100.0, 1),
            "speed_bps": speed_bps,
            "avg_speed_bps": avg_speed_bps,
            "elapsed_seconds": round(elapsed_sec, 1),
            "ewma_bps": adaptive_snap["ewma_bps"],
            "peak_bps": adaptive_snap["peak_bps"],
            "eta_seconds": eta_seconds,
            "active_connections": len(workers),
            "target_connections": adaptive_snap["target_connections"],
            "adaptive_reason": adaptive_snap["reason"],
            "keepalive_reuses": self._keepalive_reuses,
            "speed_history": adaptive_snap["history"],
            "segment_map": self._compute_segment_map() if rec.status != DownloadStatus.COMPLETED else [2] * 80,
            "workers": workers,
            "logs": logs,
            "config": {
                "initial_connections": rec.config.initial_connections,
                "max_connections": rec.config.max_connections,
                "adaptive_enabled": rec.config.adaptive_enabled,
                "speed_limit_bps": rec.config.speed_limit_bps,
            },
            "created_at": rec.created_at,
            "updated_at": rec.updated_at,
            "completed_at": rec.completed_at,
        }


class DownloadManager:
    """Top-level coordinator managing tasks, queue limits, scheduler, orphans, and power state."""

    def __init__(self, data_dir: str, default_download_dir: str) -> None:
        self.data_dir = os.path.abspath(data_dir)
        self.segments_dir = os.path.join(self.data_dir, "segments")
        self.default_download_dir = os.path.abspath(default_download_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.segments_dir, exist_ok=True)
        os.makedirs(self.default_download_dir, exist_ok=True)

        self.store = DownloadStore(os.path.join(self.data_dir, "happydownload.db"))
        self.settings: AppSettings = self.store.load_settings()

        self._lock = threading.Lock()
        self._tasks: Dict[str, DownloadTask] = {}
        self._shutdown_event = threading.Event()
        self._supervisor_wake = threading.Event()
        self._had_active_work_since_boot = False

        self._last_clipboard_raw: Optional[str] = None
        self._clipboard_candidate: Optional[str] = None
        self._dismissed_clipboard_urls: set[str] = set()
        self._recent_completions: Deque[Dict[str, object]] = deque(maxlen=10)

        self._load_persisted_tasks()
        self._supervisor_thread = threading.Thread(
            target=self._supervisor_loop,
            name="happydl-supervisor",
            daemon=True,
        )
        self._supervisor_thread.start()

    def _load_persisted_tasks(self) -> None:
        records = self.store.list_records()
        to_auto_resume: List[DownloadTask] = []
        for rec in records:
            was_active = rec.status in ACTIVE_STATUSES
            if was_active:
                if os.path.exists(rec.db_path):
                    try:
                        ss = SegmentStore(rec.db_path)
                        if rec.mode == "hls":
                            ss.reset_active_hls_to_pending()
                            _, _, rec.downloaded_bytes = ss.hls_stats()
                        else:
                            ss.reset_active_to_pending()
                            rec.downloaded_bytes = ss.total_downloaded()
                        ss.close()
                    except Exception:
                        pass
                if self.settings.auto_resume_on_startup:
                    rec.status = DownloadStatus.QUEUED
                    rec.error_message = None
                else:
                    rec.status = DownloadStatus.PAUSED
                    rec.error_message = "Recovered after restart — ready to resume without loss"
                self.store.save_record(rec)

            task = DownloadTask(
                rec,
                self.store,
                on_complete_cb=self._handle_task_completed,
                on_finish_cb=self._handle_task_finished,
            )
            self._tasks[rec.id] = task
            if was_active and self.settings.auto_resume_on_startup:
                to_auto_resume.append(task)

        if to_auto_resume:
            self._dispatch_queue()

    def _handle_task_finished(self, _task: DownloadTask) -> None:
        self._supervisor_wake.set()

    def _handle_task_completed(self, task: DownloadTask) -> None:
        self._recent_completions.append(
            {
                "id": task.record.id,
                "filename": task.record.filename,
                "total_bytes": task.record.total_bytes,
                "ts": time.time(),
            }
        )
        if self.settings.desktop_notifications:
            size_gb = (task.record.total_bytes or task.record.downloaded_bytes) / (1024 ** 3)
            show_desktop_notification(
                "Happydownload Complete",
                f"{task.record.filename} ({size_gb:.2f} GB)",
            )

    def _active_running_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._tasks.values() if t.is_running())

    def _dispatch_queue(self) -> None:
        limit = self.settings.max_concurrent_downloads
        with self._lock:
            tasks = list(self._tasks.values())
        running = sum(1 for t in tasks if t.is_running())
        queued = [t for t in tasks if t.record.status == DownloadStatus.QUEUED and not t.is_running()]
        queued.sort(key=lambda t: t.record.created_at)

        for task in queued:
            if limit > 0 and running >= limit:
                break
            self._apply_schedule_to_task(task)
            task.start()
            running += 1

    def _apply_schedule_to_task(self, task: DownloadTask) -> None:
        if not self.settings.schedule_enabled:
            task.apply_effective_rate_limit(0)
            return
        full_speed = is_in_full_speed_window(
            self.settings.schedule_full_start,
            self.settings.schedule_full_end,
        )
        cap_bps = 0 if full_speed else max(64, self.settings.schedule_day_limit_kbps) * 1024
        task.apply_effective_rate_limit(cap_bps)

    def _supervisor_loop(self) -> None:
        last_clip_check = 0.0
        while not self._shutdown_event.is_set():
            try:
                self._dispatch_queue()
                with self._lock:
                    tasks = list(self._tasks.values())

                running_tasks = [t for t in tasks if t.is_running()]
                queued_tasks = [t for t in tasks if t.record.status == DownloadStatus.QUEUED]

                for t in running_tasks:
                    self._apply_schedule_to_task(t)

                any_active = len(running_tasks) > 0
                if any_active:
                    self._had_active_work_since_boot = True

                # Sleep prevention
                set_prevent_sleep(any_active and self.settings.prevent_sleep_while_downloading)

                # Power action when all active & queued tasks finish
                if (
                    self._had_active_work_since_boot
                    and not any_active
                    and not queued_tasks
                    and self.settings.on_all_complete_action in ("sleep", "shutdown")
                ):
                    action = self.settings.on_all_complete_action
                    self.settings.on_all_complete_action = "none"
                    self.store.save_settings(self.settings)
                    self._had_active_work_since_boot = False
                    execute_power_action(action)

                # Optional clipboard link detection
                now = time.monotonic()
                if self.settings.clipboard_watch and now - last_clip_check >= 1.5:
                    last_clip_check = now
                    clip_txt = read_clipboard_text()
                    if clip_txt != self._last_clipboard_raw:
                        self._last_clipboard_raw = clip_txt
                        candidate = extract_download_url_candidate(clip_txt)
                        if candidate and candidate not in self._dismissed_clipboard_urls:
                            already = any(t.record.url == candidate for t in tasks)
                            if not already:
                                self._clipboard_candidate = candidate
            except Exception:
                pass
            self._supervisor_wake.wait(1.0)
            self._supervisor_wake.clear()
        set_prevent_sleep(False)

    def get_settings(self) -> Dict[str, object]:
        s = self.settings
        sched_full = is_in_full_speed_window(s.schedule_full_start, s.schedule_full_end) if s.schedule_enabled else True
        return {
            "max_concurrent_downloads": s.max_concurrent_downloads,
            "auto_resume_on_startup": s.auto_resume_on_startup,
            "prevent_sleep_while_downloading": s.prevent_sleep_while_downloading,
            "on_all_complete_action": s.on_all_complete_action,
            "schedule_enabled": s.schedule_enabled,
            "schedule_full_start": s.schedule_full_start,
            "schedule_full_end": s.schedule_full_end,
            "schedule_day_limit_kbps": s.schedule_day_limit_kbps,
            "schedule_currently_full_speed": sched_full,
            "clipboard_watch": s.clipboard_watch,
            "desktop_notifications": s.desktop_notifications,
            "auto_categorize_folders": s.auto_categorize_folders,
            "cookies_from_browser": s.cookies_from_browser,
        }

    def update_settings(self, patch: Dict[str, object]) -> Dict[str, object]:
        s = self.settings
        if "max_concurrent_downloads" in patch:
            s.max_concurrent_downloads = max(0, min(16, int(patch["max_concurrent_downloads"])))
        if "auto_resume_on_startup" in patch:
            s.auto_resume_on_startup = bool(patch["auto_resume_on_startup"])
        if "prevent_sleep_while_downloading" in patch:
            s.prevent_sleep_while_downloading = bool(patch["prevent_sleep_while_downloading"])
        if "on_all_complete_action" in patch:
            act = str(patch["on_all_complete_action"]).lower()
            if act in ("none", "sleep", "shutdown"):
                s.on_all_complete_action = act
        if "schedule_enabled" in patch:
            s.schedule_enabled = bool(patch["schedule_enabled"])
        if "schedule_full_start" in patch:
            s.schedule_full_start = str(patch["schedule_full_start"])[:5]
        if "schedule_full_end" in patch:
            s.schedule_full_end = str(patch["schedule_full_end"])[:5]
        if "schedule_day_limit_kbps" in patch:
            s.schedule_day_limit_kbps = max(64, int(patch["schedule_day_limit_kbps"]))
        if "clipboard_watch" in patch:
            s.clipboard_watch = bool(patch["clipboard_watch"])
            if not s.clipboard_watch:
                self._clipboard_candidate = None
        if "desktop_notifications" in patch:
            s.desktop_notifications = bool(patch["desktop_notifications"])
        if "auto_categorize_folders" in patch:
            s.auto_categorize_folders = bool(patch["auto_categorize_folders"])
        if "cookies_from_browser" in patch:
            b = str(patch["cookies_from_browser"] or "none").strip().lower()
            if b in ("none", "chrome", "edge", "firefox", "brave"):
                s.cookies_from_browser = b

        self.store.save_settings(s)
        self._dispatch_queue()
        return self.get_settings()

    def dismiss_clipboard_candidate(self, url: Optional[str] = None) -> None:
        target = url or self._clipboard_candidate
        if target:
            self._dismissed_clipboard_urls.add(target)
        if not url or url == self._clipboard_candidate:
            self._clipboard_candidate = None

    def get_clipboard_candidate(self) -> Optional[str]:
        return self._clipboard_candidate

    def get_recent_completions(self) -> List[Dict[str, object]]:
        return list(self._recent_completions)

    def scan_orphans(self) -> List[Dict[str, object]]:
        """Find leftover .part / .happydl.db* files not referenced by any known task."""
        with self._lock:
            tasks = list(self._tasks.values())

        tracked_paths = set()
        dirs_to_scan = {self.default_download_dir}
        for t in tasks:
            dirs_to_scan.add(t.record.dest_dir)
            tracked_paths.add(os.path.abspath(t.record.part_path))
            for suf in ("", "-wal", "-shm"):
                tracked_paths.add(os.path.abspath(t.record.db_path + suf))

        orphans: List[Dict[str, object]] = []
        for d in dirs_to_scan:
            if not os.path.isdir(d):
                continue
            try:
                for entry in os.scandir(d):
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if name.endswith(".part") or ".happydl.db" in name:
                        full = os.path.abspath(entry.path)
                        if full not in tracked_paths:
                            try:
                                size = entry.stat().st_size
                            except OSError:
                                size = 0
                            orphans.append({"path": full, "name": name, "bytes": size})
            except OSError:
                continue
        orphans.sort(key=lambda x: str(x["name"]))
        return orphans

    def clean_orphans(self) -> Dict[str, object]:
        orphans = self.scan_orphans()
        removed = 0
        freed_bytes = 0
        for item in orphans:
            p = str(item["path"])
            try:
                os.remove(p)
                removed += 1
                freed_bytes += int(item["bytes"])
            except OSError:
                pass
        return {"removed": removed, "freed_bytes": freed_bytes}

    def probe(
        self,
        url: str,
        dest_dir: Optional[str] = None,
        custom_headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, object]:
        target_dir = os.path.abspath(dest_dir or self.default_download_dir)
        cfg = DownloadConfig(custom_headers=custom_headers or {})
        insp = inspect_any_url(
            url,
            user_agent=cfg.user_agent,
            custom_headers=cfg.custom_headers,
            timeout=cfg.connect_timeout,
            cookies_from_browser=self.settings.cookies_from_browser,
        )
        if insp.kind == "direct" and insp.probe is not None:
            res = insp.probe
            ok, space_warn, free_bytes = check_storage_compatibility(target_dir, res.total_bytes)
            return {
                "kind": "direct",
                "url": res.original_url,
                "final_url": res.final_url,
                "filename": res.filename,
                "total_bytes": res.total_bytes,
                "accept_ranges": res.accept_ranges,
                "etag": res.etag,
                "last_modified": res.last_modified,
                "content_type": res.content_type,
                "dest_dir": target_dir,
                "storage_ok": ok,
                "storage_warning": space_warn,
                "free_bytes": free_bytes,
                "capabilities": get_capabilities(),
            }

        _, _, free_bytes = check_storage_compatibility(target_dir, None)
        videos_out = []
        for v in (insp.videos or []):
            videos_out.append(
                {
                    "id": v.id,
                    "title": v.title,
                    "page_url": v.page_url,
                    "extractor": v.extractor,
                    "duration": v.duration,
                    "thumbnail": v.thumbnail,
                    "formats": [
                        {
                            "format_id": f.format_id,
                            "url": f.url,
                            "ext": f.ext,
                            "label": f.label,
                            "protocol": f.protocol,
                            "height": f.height,
                            "tbr_kbps": f.tbr_kbps,
                            "filesize_approx": f.filesize_approx,
                            "has_video": f.has_video,
                            "has_audio": f.has_audio,
                            "http_headers": f.http_headers,
                            "audio_url": f.audio_url,
                            "audio_ext": f.audio_ext,
                            "audio_protocol": f.audio_protocol,
                            "audio_headers": f.audio_headers,
                        }
                        for f in v.formats
                    ],
                }
            )
        return {
            "kind": "videos",
            "url": url,
            "dest_dir": target_dir,
            "free_bytes": free_bytes,
            "capabilities": get_capabilities(),
            "videos": videos_out,
        }

    def _create_single_task(
        self,
        url: str,
        final_url: str,
        filename: str,
        dest_dir: str,
        total_bytes: Optional[int],
        accept_ranges: bool,
        etag: Optional[str],
        last_modified: Optional[str],
        mode: str,
        source_page_url: Optional[str],
        expected_sha256: Optional[str],
        mirrors: List[str],
        cfg: DownloadConfig,
        probe_first: bool,
        audio_url: Optional[str] = None,
    ) -> DownloadTask:
        dl_id = uuid.uuid4().hex
        effective_dir = _resolve_category_dir(dest_dir, filename, self.settings.auto_categorize_folders)
        dest_path = _unique_dest_path(effective_dir, filename)
        actual_filename = os.path.basename(dest_path)
        part_path = dest_path + ".part"
        db_path = os.path.join(self.segments_dir, f"{dl_id}.db")

        rec = DownloadRecord(
            id=dl_id,
            url=url,
            final_url=final_url,
            filename=actual_filename,
            dest_dir=effective_dir,
            dest_path=dest_path,
            part_path=part_path,
            db_path=db_path,
            total_bytes=total_bytes,
            downloaded_bytes=0,
            accept_ranges=accept_ranges,
            etag=etag,
            last_modified=last_modified,
            status=DownloadStatus.QUEUED,
            mode=mode,
            source_page_url=source_page_url,
            audio_url=audio_url,
            expected_sha256=expected_sha256.strip() if expected_sha256 else None,
            mirrors=mirrors,
            config=cfg,
        )
        self.store.save_record(rec)
        task = DownloadTask(
            rec,
            self.store,
            needs_probe=not probe_first,
            on_complete_cb=self._handle_task_completed,
            on_finish_cb=self._handle_task_finished,
        )
        with self._lock:
            self._tasks[rec.id] = task
        return task

    def create_downloads_from_url(
        self,
        url: str,
        dest_dir: Optional[str] = None,
        filename: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        max_connections: int = 16,
        initial_connections: int = 4,
        adaptive_enabled: bool = True,
        speed_limit_bps: int = 0,
        custom_headers: Optional[Dict[str, str]] = None,
        mirrors: Optional[List[str]] = None,
        mode: Optional[str] = None,
        source_page_url: Optional[str] = None,
        probe_first: bool = True,
        audio_url: Optional[str] = None,
    ) -> List[DownloadTask]:
        target_dir = os.path.abspath(dest_dir or self.default_download_dir)
        os.makedirs(target_dir, exist_ok=True)
        clean_mirrors = [m.strip() for m in (mirrors or []) if m and m.strip() and m.strip() != url]

        cfg = DownloadConfig(
            initial_connections=max(1, min(initial_connections, max_connections)),
            max_connections=max(1, min(64, max_connections)),
            adaptive_enabled=adaptive_enabled,
            speed_limit_bps=max(0, speed_limit_bps),
            custom_headers=dict(custom_headers or {}),
        )

        # Explicit mode (e.g., user chose a specific extracted video format or HLS stream)
        if mode == "hls" or not probe_first:
            chosen_mode = mode or "http_range"
            resolved_name = sanitize_filename(filename) if filename else ("video.ts" if chosen_mode == "hls" else "download.bin")
            task = self._create_single_task(
                url=url,
                final_url=url,
                filename=resolved_name,
                dest_dir=target_dir,
                total_bytes=None,
                accept_ranges=(chosen_mode == "http_range"),
                etag=None,
                last_modified=None,
                mode=chosen_mode,
                source_page_url=source_page_url,
                expected_sha256=expected_sha256,
                mirrors=clean_mirrors,
                cfg=cfg,
                probe_first=(chosen_mode == "hls"),
                audio_url=audio_url,
            )
            self.dismiss_clipboard_candidate(url)
            self._dispatch_queue()
            return [task]

        insp = inspect_any_url(
            url,
            user_agent=cfg.user_agent,
            custom_headers=cfg.custom_headers,
            timeout=cfg.connect_timeout,
            cookies_from_browser=self.settings.cookies_from_browser,
        )

        created: List[DownloadTask] = []
        if insp.kind == "direct" and insp.probe is not None:
            res = insp.probe
            resolved_name = sanitize_filename(filename) if filename else res.filename
            task = self._create_single_task(
                url=url,
                final_url=res.final_url,
                filename=resolved_name,
                dest_dir=target_dir,
                total_bytes=res.total_bytes,
                accept_ranges=res.accept_ranges,
                etag=res.etag,
                last_modified=res.last_modified,
                mode="http_range",
                source_page_url=source_page_url,
                expected_sha256=expected_sha256,
                mirrors=clean_mirrors,
                cfg=cfg,
                probe_first=True,
            )
            created.append(task)
        else:
            videos = insp.videos or []
            if not videos:
                raise FatalDownloadError("No downloadable videos found at that link.")
            multi = len(videos) > 1
            for idx, vid in enumerate(videos):
                if not vid.formats:
                    continue
                fmt = vid.formats[0]
                ext = fmt.ext or ("ts" if fmt.protocol == "hls" else "mp4")
                if fmt.protocol == "hls" and ext == "ts" and find_ffmpeg():
                    ext = "mp4"
                if filename and not multi:
                    vid_name = sanitize_filename(filename)
                    if "." not in vid_name:
                        vid_name = f"{vid_name}.{ext}"
                else:
                    base_title = sanitize_filename(vid.title, fallback=f"video_{idx + 1}")
                    vid_name = base_title if base_title.lower().endswith(f".{ext}") else f"{base_title}.{ext}"

                merged_headers = dict(cfg.custom_headers)
                for k, v in fmt.http_headers.items():
                    if k.lower() != "user-agent":
                        merged_headers.setdefault(k, v)
                vid_cfg = DownloadConfig(
                    initial_connections=cfg.initial_connections,
                    max_connections=cfg.max_connections,
                    adaptive_enabled=cfg.adaptive_enabled,
                    speed_limit_bps=cfg.speed_limit_bps,
                    user_agent=fmt.http_headers.get("User-Agent") or cfg.user_agent,
                    custom_headers=merged_headers,
                )

                if fmt.protocol == "hls":
                    task = self._create_single_task(
                        url=fmt.url,
                        final_url=fmt.url,
                        filename=vid_name,
                        dest_dir=target_dir,
                        total_bytes=None,
                        accept_ranges=True,
                        etag=None,
                        last_modified=None,
                        mode="hls",
                        source_page_url=vid.page_url,
                        expected_sha256=None,
                        mirrors=[],
                        cfg=vid_cfg,
                        probe_first=True,
                    )
                else:
                    # Probe selected direct format URL so range support & size are known
                    try:
                        p = probe_url(
                            fmt.url,
                            user_agent=vid_cfg.user_agent,
                            custom_headers=vid_cfg.custom_headers,
                            timeout=vid_cfg.connect_timeout,
                        )
                        f_url = p.final_url
                        t_bytes = p.total_bytes or fmt.filesize_approx
                        acc_r = p.accept_ranges
                        etag_v = p.etag
                        lm_v = p.last_modified
                    except Exception:
                        f_url = fmt.url
                        t_bytes = fmt.filesize_approx
                        acc_r = True
                        etag_v = None
                        lm_v = None
                    task = self._create_single_task(
                        url=fmt.url,
                        final_url=f_url,
                        filename=vid_name,
                        dest_dir=target_dir,
                        total_bytes=t_bytes,
                        accept_ranges=acc_r,
                        etag=etag_v,
                        last_modified=lm_v,
                        mode="http_range",
                        source_page_url=vid.page_url,
                        expected_sha256=expected_sha256 if not multi else None,
                        mirrors=clean_mirrors if not multi else [],
                        cfg=vid_cfg,
                        probe_first=True,
                        audio_url=fmt.audio_url,
                    )
                created.append(task)

        self.dismiss_clipboard_candidate(url)
        self._dispatch_queue()
        return created

    def create_download(
        self,
        url: str,
        dest_dir: Optional[str] = None,
        filename: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        max_connections: int = 16,
        initial_connections: int = 4,
        adaptive_enabled: bool = True,
        speed_limit_bps: int = 0,
        custom_headers: Optional[Dict[str, str]] = None,
        mirrors: Optional[List[str]] = None,
        mode: Optional[str] = None,
        source_page_url: Optional[str] = None,
        probe_first: bool = True,
        audio_url: Optional[str] = None,
    ) -> DownloadTask:
        tasks = self.create_downloads_from_url(
            url=url,
            dest_dir=dest_dir,
            filename=filename,
            expected_sha256=expected_sha256,
            max_connections=max_connections,
            initial_connections=initial_connections,
            adaptive_enabled=adaptive_enabled,
            speed_limit_bps=speed_limit_bps,
            custom_headers=custom_headers,
            mirrors=mirrors,
            mode=mode,
            source_page_url=source_page_url,
            probe_first=probe_first,
            audio_url=audio_url,
        )
        return tasks[0]

    def get_task(self, download_id: str) -> Optional[DownloadTask]:
        with self._lock:
            return self._tasks.get(download_id)

    def list_tasks(self) -> List[Dict[str, object]]:
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.record.created_at, reverse=True)
        return [t.to_dict() for t in tasks]

    def pause(self, download_id: str) -> bool:
        task = self.get_task(download_id)
        if not task:
            return False
        if task.is_running():
            task.pause()
        elif task.record.status == DownloadStatus.QUEUED:
            task._set_status(DownloadStatus.PAUSED)
        return True

    def pause_all(self) -> int:
        with self._lock:
            tasks = list(self._tasks.values())
        count = 0
        for t in tasks:
            if t.is_running() or t.record.status == DownloadStatus.QUEUED:
                self.pause(t.record.id)
                count += 1
        return count

    def resume(self, download_id: str) -> bool:
        task = self.get_task(download_id)
        if not task:
            return False
        if task.record.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED):
            return False
        limit = self.settings.max_concurrent_downloads
        if limit > 0 and self._active_running_count() >= limit:
            task._set_status(DownloadStatus.QUEUED)
        else:
            self._apply_schedule_to_task(task)
            task.start()
        return True

    def resume_all(self) -> int:
        with self._lock:
            tasks = list(self._tasks.values())
        count = 0
        for t in tasks:
            if t.record.status in (DownloadStatus.PAUSED, DownloadStatus.FAILED):
                self.resume(t.record.id)
                count += 1
        return count

    def retry_failed(self) -> int:
        with self._lock:
            tasks = list(self._tasks.values())
        count = 0
        for t in tasks:
            if t.record.status == DownloadStatus.FAILED:
                t.record.error_message = None
                self.resume(t.record.id)
                count += 1
        return count

    def clear_completed(self) -> int:
        with self._lock:
            ids = [
                dl_id
                for dl_id, t in self._tasks.items()
                if t.record.status in (DownloadStatus.COMPLETED, DownloadStatus.CANCELLED)
            ]
        count = 0
        for dl_id in ids:
            if self.delete(dl_id, remove_file=False):
                count += 1
        return count

    def cancel(self, download_id: str) -> bool:
        task = self.get_task(download_id)
        if not task:
            return False
        if task.is_running():
            task.cancel()
        else:
            task._cleanup_partial_files()
            task._set_status(DownloadStatus.CANCELLED)
        return True

    def delete(self, download_id: str, remove_file: bool = False) -> bool:
        task = self.get_task(download_id)
        if not task:
            return False
        if task.is_running():
            task.pause()
            if task._thread:
                task._thread.join(timeout=5.0)
        task._cleanup_partial_files()
        if remove_file and os.path.exists(task.record.dest_path):
            try:
                os.remove(task.record.dest_path)
            except OSError:
                pass
        with self._lock:
            self._tasks.pop(download_id, None)
        self.store.delete_record(download_id)
        return True

    def verify_completed(self, download_id: str) -> Optional[str]:
        task = self.get_task(download_id)
        if not task or not os.path.exists(task.record.dest_path):
            return None
        digest = compute_sha256_streaming(task.record.dest_path)
        if digest:
            task.record.actual_sha256 = digest
            self.store.save_record(task.record)
        return digest

    def start_verify_async(self, download_id: str) -> bool:
        task = self.get_task(download_id)
        if not task or task.is_running() or not os.path.exists(task.record.dest_path):
            return False

        def _worker() -> None:
            prev_status = task.record.status
            task._verify_progress = 0.0
            task._set_status(DownloadStatus.VERIFYING)
            task.log("Running SHA-256 integrity verification...")
            digest = compute_sha256_streaming(
                task.record.dest_path,
                progress_cb=task._on_verify_progress,
            )
            if digest:
                task.record.actual_sha256 = digest
                if task.record.expected_sha256:
                    if digest.lower() == task.record.expected_sha256.strip().lower():
                        task.log(f"SHA-256 verified match: {digest}", "info")
                        task._set_status(DownloadStatus.COMPLETED)
                    else:
                        msg = f"SHA-256 mismatch! Expected {task.record.expected_sha256}, got {digest}"
                        task.log(msg, "error")
                        task._set_status(DownloadStatus.FAILED, msg)
                else:
                    task.log(f"SHA-256 computed: {digest}", "info")
                    task._set_status(prev_status)
            else:
                task._set_status(prev_status)

        threading.Thread(target=_worker, daemon=True, name=f"happydl-verify-{download_id[:6]}").start()
        return True

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self._supervisor_wake.set()
        set_prevent_sleep(False)
        with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            if t.is_running():
                t.pause()
        for t in tasks:
            if t._thread and t._thread.is_alive():
                t._thread.join(timeout=6.0)
        self.store.close()
