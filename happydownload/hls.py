"""HLS (.m3u8) playlist parser and non-DRM stream validator."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from .models import ExtractedFormat
from .network import FatalDownloadError, KeepAliveClient, build_base_headers

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("([^"]*)"|[^,\s]+)')


@dataclass
class HlsPlaylistInfo:
    playlist_url: str
    is_master: bool
    variants: List[ExtractedFormat]
    segment_urls: List[str]
    total_duration: float
    is_fmp4: bool = False


def _parse_attrs(line: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(line):
        key = m.group(1).upper()
        val = m.group(3) if m.group(3) is not None else m.group(2)
        attrs[key] = val
    return attrs


def fetch_text_url(
    url: str,
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    max_bytes: int = 8 * 1024 * 1024,
) -> Tuple[str, str, Dict[str, str]]:
    """Fetch a text resource (HTML / M3U8) following redirects; returns `(final_url, text, headers)`."""
    client = KeepAliveClient()
    headers = build_base_headers(user_agent, custom_headers, keep_alive=False)
    headers["Accept"] = "text/html,application/xhtml+xml,application/vnd.apple.mpegurl,*/*"
    stream = client.open_request("GET", url, headers=headers, timeout=timeout)
    try:
        chunks = []
        read_len = 0
        while read_len < max_bytes:
            piece = stream.read(min(65536, max_bytes - read_len))
            if not piece:
                break
            chunks.append(piece)
            read_len += len(piece)
        raw = b"".join(chunks)
        text = raw.decode("utf-8", errors="replace")
        return stream.final_url, text, stream.headers
    finally:
        stream.close(force_close=True)
        client.close()


def parse_m3u8_text(playlist_url: str, text: str, headers_for_segments: Optional[Dict[str, str]] = None) -> HlsPlaylistInfo:
    """Parse an M3U8 master or media playlist into variants or segment URLs."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or not any(ln.startswith("#EXTM3U") for ln in lines[:3]):
        raise FatalDownloadError("Invalid HLS playlist (missing #EXTM3U header)")

    variants: List[ExtractedFormat] = []
    segment_urls: List[str] = []
    total_duration = 0.0
    is_fmp4 = False

    i = 0
    pending_stream_inf: Optional[Dict[str, str]] = None
    pending_duration = 0.0

    while i < len(lines):
        ln = lines[i]
        i += 1

        if ln.startswith("#EXT-X-KEY"):
            attrs = _parse_attrs(ln.split(":", 1)[1] if ":" in ln else "")
            method = attrs.get("METHOD", "NONE").upper()
            if method != "NONE":
                raise FatalDownloadError(
                    f"Encrypted/DRM HLS streams (METHOD={method}) are not supported."
                )
            continue

        if ln.startswith("#EXT-X-MAP"):
            attrs = _parse_attrs(ln.split(":", 1)[1] if ":" in ln else "")
            uri = attrs.get("URI")
            if uri:
                is_fmp4 = True
                segment_urls.append(urljoin(playlist_url, uri))
            continue

        if ln.startswith("#EXT-X-STREAM-INF"):
            pending_stream_inf = _parse_attrs(ln.split(":", 1)[1] if ":" in ln else "")
            continue

        if ln.startswith("#EXTINF:"):
            dur_str = ln.split(":", 1)[1].split(",", 1)[0].strip()
            try:
                pending_duration = max(0.0, float(dur_str))
            except ValueError:
                pending_duration = 0.0
            continue

        if ln.startswith("#"):
            continue

        # Non-comment URI line
        abs_url = urljoin(playlist_url, ln)
        if pending_stream_inf is not None:
            bw_raw = pending_stream_inf.get("BANDWIDTH", "0")
            bw = int(bw_raw) if bw_raw.isdigit() else 0
            res = pending_stream_inf.get("RESOLUTION", "")
            height: Optional[int] = None
            if "x" in res.lower():
                try:
                    height = int(res.lower().split("x", 1)[1])
                except ValueError:
                    height = None
            tbr = round(bw / 1000.0, 1) if bw > 0 else None
            label_parts = []
            if height:
                label_parts.append(f"{height}p")
            elif res:
                label_parts.append(res)
            else:
                label_parts.append("HLS")
            if tbr:
                label_parts.append(f"{int(tbr)} kbps")
            variants.append(
                ExtractedFormat(
                    format_id=f"hls-{len(variants)}",
                    url=abs_url,
                    ext="ts",
                    label=" • ".join(label_parts) + " (HLS)",
                    protocol="hls",
                    height=height,
                    tbr_kbps=tbr,
                    has_video=True,
                    has_audio=True,
                    http_headers=dict(headers_for_segments or {}),
                )
            )
            pending_stream_inf = None
        else:
            segment_urls.append(abs_url)
            total_duration += pending_duration
            pending_duration = 0.0

    if variants:
        variants.sort(key=lambda f: (f.height or 0, f.tbr_kbps or 0.0), reverse=True)
        return HlsPlaylistInfo(
            playlist_url=playlist_url,
            is_master=True,
            variants=variants,
            segment_urls=[],
            total_duration=0.0,
        )

    if not segment_urls:
        raise FatalDownloadError("HLS playlist contained no playable media segments.")

    return HlsPlaylistInfo(
        playlist_url=playlist_url,
        is_master=False,
        variants=[],
        segment_urls=segment_urls,
        total_duration=total_duration,
        is_fmp4=is_fmp4,
    )


def resolve_hls_media_playlist(
    url: str,
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
) -> HlsPlaylistInfo:
    """Fetch an M3U8 URL; if master playlist, automatically resolve its highest-quality variant."""
    final_url, text, _ = fetch_text_url(url, user_agent=user_agent, custom_headers=custom_headers, timeout=timeout)
    info = parse_m3u8_text(final_url, text, headers_for_segments=custom_headers)
    if info.is_master and info.variants:
        best = info.variants[0]
        final_v, v_text, _ = fetch_text_url(
            best.url, user_agent=user_agent, custom_headers=custom_headers, timeout=timeout
        )
        return parse_m3u8_text(final_v, v_text, headers_for_segments=custom_headers)
    return info
