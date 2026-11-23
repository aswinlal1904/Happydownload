"""Persistent SQLite storage, Windows sparse-file random-access I/O, and verification."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .models import AppSettings, DownloadConfig, DownloadRecord, DownloadStatus, Segment, SegmentStatus

FSCTL_SET_SPARSE = 0x000900C4
FAT32_MAX_FILE_BYTES = (4 * 1024 * 1024 * 1024) - 1


def enable_windows_sparse_file(file_obj) -> bool:
    """Mark a file handle as sparse on Windows NTFS so high-offset writes don't zero-fill."""
    if sys.platform != "win32":
        return False
    try:
        import msvcrt

        handle = msvcrt.get_osfhandle(file_obj.fileno())
        bytes_returned = ctypes.c_ulong(0)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.DeviceIoControl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p,
        ]
        k32.DeviceIoControl.restype = ctypes.c_int
        ok = k32.DeviceIoControl(
            ctypes.c_void_p(handle),
            FSCTL_SET_SPARSE,
            None,
            0,
            None,
            0,
            ctypes.byref(bytes_returned),
            None,
        )
        return bool(ok)
    except Exception:
        return False


def get_filesystem_name(path: str) -> Optional[str]:
    """Return volume filesystem name (e.g., 'NTFS', 'exFAT', 'FAT32') on Windows."""
    if sys.platform != "win32":
        return None
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return None
        root = drive + "\\"
        fs_buf = ctypes.create_unicode_buffer(64)
        vol_buf = ctypes.create_unicode_buffer(256)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            vol_buf,
            len(vol_buf),
            None,
            None,
            None,
            fs_buf,
            len(fs_buf),
        )
        return fs_buf.value if ok else None
    except Exception:
        return None


def check_storage_compatibility(
    dest_dir: str,
    total_bytes: Optional[int],
    already_on_disk: int = 0,
) -> Tuple[bool, Optional[str], int]:
    """Check free space and filesystem 4 GB limit. Returns (ok, warning_or_error, free_bytes)."""
    os.makedirs(dest_dir, exist_ok=True)
    usage = shutil.disk_usage(dest_dir)
    free_bytes = usage.free

    if total_bytes is not None:
        fs_name = get_filesystem_name(dest_dir)
        if fs_name and fs_name.upper() == "FAT32" and total_bytes > FAT32_MAX_FILE_BYTES:
            return (
                False,
                f"Destination drive uses FAT32 which cannot store files larger than 4 GB ({total_bytes / (1024**3):.2f} GB required).",
                free_bytes,
            )
        remaining_needed = max(0, total_bytes - already_on_disk)
        # Keep a small 64 MiB safety margin for metadata & filesystem overhead
        if free_bytes < remaining_needed + (64 * 1024 * 1024):
            return (
                False,
                (
                    f"Insufficient disk space in {dest_dir}: "
                    f"need {remaining_needed / (1024**3):.2f} GB more, "
                    f"only {free_bytes / (1024**3):.2f} GB free."
                ),
                free_bytes,
            )

    return True, None, free_bytes


class PartFileHandlePool:
    """Thread-safe sparse `.part` file manager with per-thread non-overlapping write handles."""

    def __init__(self, part_path: str, total_bytes: Optional[int]) -> None:
        self.part_path = part_path
        self.total_bytes = total_bytes
        self._local = threading.local()
        self._handles: List[object] = []
        self._lock = threading.Lock()
        self.is_sparse = False
        self._ensure_initialized()

    def _ensure_initialized(self) -> None:
        parent = os.path.dirname(os.path.abspath(self.part_path))
        os.makedirs(parent, exist_ok=True)
        existed = os.path.exists(self.part_path)
        mode = "r+b" if existed else "w+b"
        with open(self.part_path, mode) as f:
            # Mark file sparse on NTFS without calling f.truncate(total_bytes),
            # because Python's _chsize_s on Windows synchronously zero-fills multi-GB extensions.
            self.is_sparse = enable_windows_sparse_file(f)

    def _get_handle(self):
        h = getattr(self._local, "fh", None)
        if h is None or h.closed:
            h = open(self.part_path, "r+b", buffering=0)
            self._local.fh = h
            with self._lock:
                self._handles.append(h)
        return h

    def write_at(self, offset: int, data: bytes) -> None:
        if not data:
            return
        fh = self._get_handle()
        fh.seek(offset)
        view = memoryview(data)
        written = 0
        while written < len(view):
            n = fh.write(view[written:])
            if n is None or n <= 0:
                raise IOError(f"Short write to {self.part_path} at offset {offset}")
            written += n

    def flush_thread_handle(self) -> None:
        fh = getattr(self._local, "fh", None)
        if fh is not None and not fh.closed:
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass

    def close_all(self) -> None:
        with self._lock:
            for fh in self._handles:
                try:
                    if not fh.closed:
                        fh.flush()
                        os.fsync(fh.fileno())
                        fh.close()
                except Exception:
                    pass
            self._handles.clear()
        if hasattr(self._local, "fh"):
            self._local.fh = None


class SegmentStore:
    """Per-download crash-proof SQLite WAL store tracking every byte segment."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS segments (
                    segment_id INTEGER PRIMARY KEY,
                    start_byte INTEGER NOT NULL,
                    end_byte INTEGER NOT NULL,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending'
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hls_segments (
                    seg_idx INTEGER PRIMARY KEY,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    bytes_written INTEGER NOT NULL DEFAULT 0,
                    assembled INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def has_segments(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM segments").fetchone()
            return bool(row and row["cnt"] > 0)

    def get_meta(self) -> Dict[str, Optional[str]]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM meta").fetchall()
            return {r["key"]: r["value"] for r in rows}

    def set_meta(self, mapping: Dict[str, Optional[str]]) -> None:
        with self._lock, self._conn:
            for k, v in mapping.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    (k, None if v is None else str(v)),
                )

    def initialize_segments(
        self,
        total_bytes: Optional[int],
        accept_ranges: bool,
        segment_size_bytes: int,
    ) -> List[Segment]:
        segments: List[Segment] = []
        if total_bytes is None or not accept_ranges or total_bytes <= 0:
            end_byte = (total_bytes - 1) if (total_bytes is not None and total_bytes > 0) else -1
            segments.append(Segment(0, 0, end_byte, 0, SegmentStatus.PENDING))
        else:
            if segment_size_bytes <= 0:
                if total_bytes < 64 * 1024 * 1024:
                    seg_size = 2 * 1024 * 1024
                elif total_bytes < 1024 * 1024 * 1024:
                    seg_size = 8 * 1024 * 1024
                elif total_bytes < 20 * 1024 * 1024 * 1024:
                    seg_size = 16 * 1024 * 1024
                else:
                    seg_size = 32 * 1024 * 1024
            else:
                seg_size = max(256 * 1024, segment_size_bytes)
            # Keep segment count bounded cleanly even for 500+ GB files
            if total_bytes // seg_size > 12000:
                seg_size = max(seg_size, total_bytes // 10000)
            offset = 0
            seg_id = 0
            while offset < total_bytes:
                end = min(total_bytes - 1, offset + seg_size - 1)
                segments.append(Segment(seg_id, offset, end, 0, SegmentStatus.PENDING))
                offset = end + 1
                seg_id += 1

        with self._lock, self._conn:
            self._conn.execute("DELETE FROM segments")
            self._conn.executemany(
                "INSERT INTO segments (segment_id, start_byte, end_byte, downloaded_bytes, status) VALUES (?, ?, ?, ?, ?)",
                [(s.segment_id, s.start_byte, s.end_byte, s.downloaded_bytes, s.status.value) for s in segments],
            )
        return segments

    def reset_active_to_pending(self) -> None:
        """Reset interrupted active segments back to pending while keeping exact downloaded_bytes."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE segments SET status = 'pending' WHERE status = 'active'"
            )
            # Repair any segment whose downloaded_bytes already reached its full length
            self._conn.execute(
                """
                UPDATE segments
                SET status = 'done'
                WHERE end_byte >= 0 AND downloaded_bytes >= (end_byte - start_byte + 1)
                """
            )

    def reset_unknown_size_stream(self) -> None:
        """If server does not support Range, restart segment 0 from byte 0 cleanly."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE segments SET downloaded_bytes = 0, status = 'pending'"
            )

    def list_segments(self) -> List[Segment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT segment_id, start_byte, end_byte, downloaded_bytes, status FROM segments ORDER BY segment_id"
            ).fetchall()
            return [
                Segment(
                    segment_id=r["segment_id"],
                    start_byte=r["start_byte"],
                    end_byte=r["end_byte"],
                    downloaded_bytes=r["downloaded_bytes"],
                    status=SegmentStatus(r["status"]),
                )
                for r in rows
            ]

    def total_downloaded(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(downloaded_bytes), 0) AS total FROM segments"
            ).fetchone()
            return int(row["total"]) if row else 0

    def claim_next_segment(self, min_steal_remaining: int = 2 * 1024 * 1024) -> Optional[Segment]:
        """Claim a pending segment, or work-steal half of the largest slow active segment."""
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT segment_id, start_byte, end_byte, downloaded_bytes, status
                FROM segments
                WHERE status = 'pending'
                ORDER BY segment_id ASC
                LIMIT 1
                """
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE segments SET status = 'active' WHERE segment_id = ?",
                    (row["segment_id"],),
                )
                return Segment(
                    segment_id=row["segment_id"],
                    start_byte=row["start_byte"],
                    end_byte=row["end_byte"],
                    downloaded_bytes=row["downloaded_bytes"],
                    status=SegmentStatus.ACTIVE,
                )

            # Work stealing: split largest active segment if enough remaining bytes
            candidates = self._conn.execute(
                """
                SELECT segment_id, start_byte, end_byte, downloaded_bytes
                FROM segments
                WHERE status = 'active' AND end_byte >= 0
                """
            ).fetchall()
            best = None
            best_rem = 0
            for c in candidates:
                rem = (c["end_byte"] - c["start_byte"] + 1) - c["downloaded_bytes"]
                if rem > best_rem:
                    best_rem = rem
                    best = c

            if best is None or best_rem < min_steal_remaining * 2:
                return None

            # Split remaining tail into second half for the idle worker
            current_pos = best["start_byte"] + best["downloaded_bytes"]
            half = best_rem // 2
            new_first_end = current_pos + half - 1
            new_second_start = new_first_end + 1
            new_second_end = best["end_byte"]

            max_row = self._conn.execute("SELECT COALESCE(MAX(segment_id), 0) AS mx FROM segments").fetchone()
            next_id = int(max_row["mx"]) + 1

            self._conn.execute(
                "UPDATE segments SET end_byte = ? WHERE segment_id = ?",
                (new_first_end, best["segment_id"]),
            )
            self._conn.execute(
                "INSERT INTO segments (segment_id, start_byte, end_byte, downloaded_bytes, status) VALUES (?, ?, ?, 0, 'active')",
                (next_id, new_second_start, new_second_end),
            )
            return Segment(
                segment_id=next_id,
                start_byte=new_second_start,
                end_byte=new_second_end,
                downloaded_bytes=0,
                status=SegmentStatus.ACTIVE,
            )

    def get_segment_end(self, segment_id: int) -> int:
        """Read current end_byte (which may shrink if another worker stole the tail)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT end_byte FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            return int(row["end_byte"]) if row else -1

    def checkpoint_segment(
        self,
        segment_id: int,
        downloaded_bytes: int,
        status: Optional[SegmentStatus] = None,
    ) -> None:
        with self._lock, self._conn:
            if status is None:
                self._conn.execute(
                    "UPDATE segments SET downloaded_bytes = ? WHERE segment_id = ?",
                    (downloaded_bytes, segment_id),
                )
            else:
                self._conn.execute(
                    "UPDATE segments SET downloaded_bytes = ?, status = ? WHERE segment_id = ?",
                    (downloaded_bytes, status.value, segment_id),
                )

    def all_done(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS rem FROM segments WHERE status != 'done'"
            ).fetchone()
            return bool(row and row["rem"] == 0)

    # --- HLS Segment Queue & In-Order Assembly Tracking ---

    def has_hls_segments(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS cnt FROM hls_segments").fetchone()
            return bool(row and row["cnt"] > 0)

    def initialize_hls_segments(self, urls: List[str]) -> int:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM hls_segments")
            self._conn.executemany(
                "INSERT INTO hls_segments (seg_idx, url, status, bytes_written, assembled) VALUES (?, ?, 'pending', 0, 0)",
                [(idx, u) for idx, u in enumerate(urls)],
            )
        return len(urls)

    def reset_active_hls_to_pending(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hls_segments SET status = 'pending' WHERE status = 'active'"
            )

    def claim_next_hls_segment(self) -> Optional[Tuple[int, str]]:
        with self._lock, self._conn:
            row = self._conn.execute(
                """
                SELECT seg_idx, url
                FROM hls_segments
                WHERE status = 'pending'
                ORDER BY seg_idx ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "UPDATE hls_segments SET status = 'active' WHERE seg_idx = ?",
                (row["seg_idx"],),
            )
            return int(row["seg_idx"]), str(row["url"])

    def mark_hls_segment_done(self, seg_idx: int, bytes_written: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hls_segments SET status = 'done', bytes_written = ? WHERE seg_idx = ?",
                (bytes_written, seg_idx),
            )

    def mark_hls_segment_pending(self, seg_idx: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hls_segments SET status = 'pending' WHERE seg_idx = ? AND assembled = 0",
                (seg_idx,),
            )

    def get_ready_unassembled_hls(self) -> List[int]:
        """Return contiguous `seg_idx` values ready to append in strict order into `.part`."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT seg_idx, status
                FROM hls_segments
                WHERE assembled = 0
                ORDER BY seg_idx ASC
                """
            ).fetchall()
            ready: List[int] = []
            for r in rows:
                if r["status"] != "done":
                    break
                ready.append(int(r["seg_idx"]))
            return ready

    def mark_hls_assembled(self, seg_idx: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE hls_segments SET assembled = 1 WHERE seg_idx = ?",
                (seg_idx,),
            )

    def hls_stats(self) -> Tuple[int, int, int]:
        """Return `(total_segments, done_segments, total_bytes_written)`."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_cnt,
                    SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_cnt,
                    COALESCE(SUM(bytes_written), 0) AS total_bytes
                FROM hls_segments
                """
            ).fetchone()
            if not row:
                return 0, 0, 0
            return int(row["total_cnt"] or 0), int(row["done_cnt"] or 0), int(row["total_bytes"] or 0)

    def hls_status_list(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status FROM hls_segments ORDER BY seg_idx ASC"
            ).fetchall()
            return [str(r["status"]) for r in rows]

    def all_hls_done(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS rem FROM hls_segments WHERE status != 'done' OR assembled = 0"
            ).fetchone()
            return bool(row and row["rem"] == 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


class DownloadStore:
    """Central SQLite store persisting all download tasks and configuration across restarts."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS downloads (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    final_url TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    dest_dir TEXT NOT NULL,
                    dest_path TEXT NOT NULL,
                    part_path TEXT NOT NULL,
                    db_path TEXT NOT NULL,
                    total_bytes INTEGER,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    accept_ranges INTEGER NOT NULL DEFAULT 0,
                    etag TEXT,
                    last_modified TEXT,
                    status TEXT NOT NULL,
                    expected_sha256 TEXT,
                    actual_sha256 TEXT,
                    error_message TEXT,
                    mirrors_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    config_json TEXT NOT NULL
                )
                """
            )
            # Migrate existing DBs cleanly
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(downloads)").fetchall()}
            if "mirrors_json" not in cols:
                self._conn.execute("ALTER TABLE downloads ADD COLUMN mirrors_json TEXT NOT NULL DEFAULT '[]'")
            if "mode" not in cols:
                self._conn.execute("ALTER TABLE downloads ADD COLUMN mode TEXT NOT NULL DEFAULT 'http_range'")
            if "source_page_url" not in cols:
                self._conn.execute("ALTER TABLE downloads ADD COLUMN source_page_url TEXT")
            if "audio_url" not in cols:
                self._conn.execute("ALTER TABLE downloads ADD COLUMN audio_url TEXT")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    settings_json TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _serialize_config(cfg: DownloadConfig) -> str:
        return json.dumps(
            {
                "initial_connections": cfg.initial_connections,
                "min_connections": cfg.min_connections,
                "max_connections": cfg.max_connections,
                "adaptive_enabled": cfg.adaptive_enabled,
                "segment_size_bytes": cfg.segment_size_bytes,
                "read_buffer_bytes": cfg.read_buffer_bytes,
                "checkpoint_interval_bytes": cfg.checkpoint_interval_bytes,
                "connect_timeout": cfg.connect_timeout,
                "read_timeout": cfg.read_timeout,
                "stall_timeout": cfg.stall_timeout,
                "stall_min_bps": cfg.stall_min_bps,
                "max_retries_per_segment": cfg.max_retries_per_segment,
                "speed_limit_bps": cfg.speed_limit_bps,
                "user_agent": cfg.user_agent,
                "custom_headers": cfg.custom_headers,
            }
        )

    @staticmethod
    def _deserialize_config(raw: str) -> DownloadConfig:
        data = json.loads(raw)
        return DownloadConfig(**data)

    def _row_to_record(self, r: sqlite3.Row) -> DownloadRecord:
        keys = set(r.keys())
        mirrors_raw = r["mirrors_json"] if "mirrors_json" in keys else "[]"
        try:
            mirrors = json.loads(mirrors_raw or "[]")
        except Exception:
            mirrors = []
        return DownloadRecord(
            id=r["id"],
            url=r["url"],
            final_url=r["final_url"],
            filename=r["filename"],
            dest_dir=r["dest_dir"],
            dest_path=r["dest_path"],
            part_path=r["part_path"],
            db_path=r["db_path"],
            total_bytes=r["total_bytes"],
            downloaded_bytes=r["downloaded_bytes"],
            accept_ranges=bool(r["accept_ranges"]),
            etag=r["etag"],
            last_modified=r["last_modified"],
            status=DownloadStatus(r["status"]),
            mode=r["mode"] if "mode" in keys and r["mode"] else "http_range",
            source_page_url=r["source_page_url"] if "source_page_url" in keys else None,
            audio_url=r["audio_url"] if "audio_url" in keys else None,
            expected_sha256=r["expected_sha256"],
            actual_sha256=r["actual_sha256"],
            error_message=r["error_message"],
            mirrors=mirrors,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            completed_at=r["completed_at"],
            config=self._deserialize_config(r["config_json"]),
        )

    def save_record(self, rec: DownloadRecord) -> None:
        rec.updated_at = time.time()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO downloads (
                    id, url, final_url, filename, dest_dir, dest_path, part_path, db_path,
                    total_bytes, downloaded_bytes, accept_ranges, etag, last_modified,
                    status, mode, source_page_url, audio_url, expected_sha256, actual_sha256, error_message, mirrors_json,
                    created_at, updated_at, completed_at, config_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.id,
                    rec.url,
                    rec.final_url,
                    rec.filename,
                    rec.dest_dir,
                    rec.dest_path,
                    rec.part_path,
                    rec.db_path,
                    rec.total_bytes,
                    rec.downloaded_bytes,
                    1 if rec.accept_ranges else 0,
                    rec.etag,
                    rec.last_modified,
                    rec.status.value,
                    rec.mode,
                    rec.source_page_url,
                    rec.audio_url,
                    rec.expected_sha256,
                    rec.actual_sha256,
                    rec.error_message,
                    json.dumps(rec.mirrors),
                    rec.created_at,
                    rec.updated_at,
                    rec.completed_at,
                    self._serialize_config(rec.config),
                ),
            )

    def load_settings(self) -> AppSettings:
        with self._lock:
            row = self._conn.execute("SELECT settings_json FROM app_settings WHERE id = 1").fetchone()
            if not row:
                return AppSettings()
            try:
                data = json.loads(row["settings_json"])
                valid = {k: v for k, v in data.items() if k in AppSettings.__dataclass_fields__}
                return AppSettings(**valid)
            except Exception:
                return AppSettings()

    def save_settings(self, settings: AppSettings) -> None:
        payload = json.dumps(
            {
                "max_concurrent_downloads": settings.max_concurrent_downloads,
                "auto_resume_on_startup": settings.auto_resume_on_startup,
                "prevent_sleep_while_downloading": settings.prevent_sleep_while_downloading,
                "on_all_complete_action": settings.on_all_complete_action,
                "schedule_enabled": settings.schedule_enabled,
                "schedule_full_start": settings.schedule_full_start,
                "schedule_full_end": settings.schedule_full_end,
                "schedule_day_limit_kbps": settings.schedule_day_limit_kbps,
                "clipboard_watch": settings.clipboard_watch,
                "desktop_notifications": settings.desktop_notifications,
                "auto_categorize_folders": settings.auto_categorize_folders,
                "cookies_from_browser": settings.cookies_from_browser,
            }
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO app_settings (id, settings_json) VALUES (1, ?)",
                (payload,),
            )

    def list_records(self) -> List[DownloadRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM downloads ORDER BY created_at DESC"
            ).fetchall()
            return [self._row_to_record(r) for r in rows]

    def delete_record(self, download_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def compute_sha256_streaming(
    file_path: str,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """Stream-hash a massive file in 4 MiB chunks using constant memory."""
    total_size = os.path.getsize(file_path)
    hasher = hashlib.sha256()
    read_so_far = 0
    chunk_size = 4 * 1024 * 1024
    with open(file_path, "rb") as f:
        while True:
            if cancel_check and cancel_check():
                return None
            chunk = f.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            read_so_far += len(chunk)
            if progress_cb:
                progress_cb(read_so_far, total_size)
    return hasher.hexdigest()

