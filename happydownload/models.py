"""Core data models and configuration for Happydownload."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import time
from typing import Dict, List, Optional


class DownloadStatus(str, Enum):
    QUEUED = "queued"
    PROBING = "probing"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    PAUSED = "paused"
    WAITING_NETWORK = "waiting_network"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SegmentStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"


@dataclass
class ProbeResult:
    original_url: str
    final_url: str
    filename: str
    total_bytes: Optional[int]
    accept_ranges: bool
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_type: Optional[str] = None
    status_code: int = 200
    headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class Segment:
    segment_id: int
    start_byte: int
    end_byte: int  # inclusive; -1 if unknown total size
    downloaded_bytes: int = 0
    status: SegmentStatus = SegmentStatus.PENDING

    @property
    def total_bytes(self) -> Optional[int]:
        if self.end_byte < 0:
            return None
        return self.end_byte - self.start_byte + 1

    @property
    def current_offset(self) -> int:
        return self.start_byte + self.downloaded_bytes

    @property
    def remaining_bytes(self) -> Optional[int]:
        if self.end_byte < 0:
            return None
        return max(0, (self.end_byte - self.start_byte + 1) - self.downloaded_bytes)

    @property
    def is_complete(self) -> bool:
        if self.end_byte < 0:
            return self.status == SegmentStatus.DONE
        return self.downloaded_bytes >= (self.end_byte - self.start_byte + 1)


@dataclass
class WorkerSnapshot:
    worker_id: int
    segment_id: Optional[int]
    speed_bps: float
    bytes_session: int
    state: str
    retries: int = 0
    last_error: Optional[str] = None


@dataclass
class DownloadConfig:
    initial_connections: int = 4
    min_connections: int = 1
    max_connections: int = 16
    adaptive_enabled: bool = True
    segment_size_bytes: int = 0  # 0 = Auto dynamic sizing based on file size
    read_buffer_bytes: int = 256 * 1024  # 256 KiB stream reads
    checkpoint_interval_bytes: int = 512 * 1024  # Flush SQLite progress every 512 KiB
    connect_timeout: float = 15.0
    read_timeout: float = 20.0
    stall_timeout: float = 12.0
    stall_min_bps: float = 2048.0  # < 2 KB/s for stall_timeout triggers recovery
    max_retries_per_segment: int = 25
    speed_limit_bps: int = 0  # 0 = unlimited
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
    custom_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class AppSettings:
    max_concurrent_downloads: int = 2  # 0 = unlimited
    auto_resume_on_startup: bool = True
    prevent_sleep_while_downloading: bool = True
    on_all_complete_action: str = "none"  # none | sleep | shutdown
    schedule_enabled: bool = False
    schedule_full_start: str = "23:00"  # HH:MM local time for unlimited speed
    schedule_full_end: str = "07:00"
    schedule_day_limit_kbps: int = 1024  # Limit outside full-speed window
    clipboard_watch: bool = False
    desktop_notifications: bool = True
    auto_categorize_folders: bool = False
    cookies_from_browser: str = "none"  # none | chrome | edge | firefox | brave


@dataclass
class ExtractedFormat:
    format_id: str
    url: str
    ext: str
    label: str
    protocol: str = "http"  # "http" | "hls"
    height: Optional[int] = None
    tbr_kbps: Optional[float] = None
    filesize_approx: Optional[int] = None
    has_video: bool = True
    has_audio: bool = True
    http_headers: Dict[str, str] = field(default_factory=dict)
    audio_url: Optional[str] = None
    audio_ext: Optional[str] = None
    audio_protocol: str = "http"
    audio_headers: Dict[str, str] = field(default_factory=dict)


@dataclass
class ExtractedVideo:
    id: str
    title: str
    page_url: str
    extractor: str
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    formats: List[ExtractedFormat] = field(default_factory=list)


@dataclass
class DownloadRecord:
    id: str
    url: str
    final_url: str
    filename: str
    dest_dir: str
    dest_path: str
    part_path: str
    db_path: str
    total_bytes: Optional[int]
    downloaded_bytes: int
    accept_ranges: bool
    etag: Optional[str]
    last_modified: Optional[str]
    status: DownloadStatus
    mode: str = "http_range"  # "http_range" | "hls"
    source_page_url: Optional[str] = None
    audio_url: Optional[str] = None
    expected_sha256: Optional[str] = None
    actual_sha256: Optional[str] = None
    error_message: Optional[str] = None
    mirrors: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    config: DownloadConfig = field(default_factory=DownloadConfig)
