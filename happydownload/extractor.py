"""Web video page, playlist, HTML5, and HLS stream extractor (yt-dlp + built-in fallback)."""

from __future__ import annotations

from dataclasses import dataclass
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

from .hls import fetch_text_url, parse_m3u8_text
from .models import ExtractedFormat, ExtractedVideo, ProbeResult
from .network import FatalDownloadError, probe_url, sanitize_filename

_BINARY_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts",
    ".mp3", ".flac", ".wav", ".aac", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".xz", ".bz2", ".iso", ".img",
    ".exe", ".msi", ".dmg", ".deb", ".rpm", ".apk", ".bin", ".pdf",
}

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:title|twitter:title)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_META_VIDEO_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_TAG_SRC_RE = re.compile(
    r'<(?:video|source)\b[^>]*\bsrc=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_IFRAME_SRC_RE = re.compile(
    r'<iframe\b[^>]*\bsrc=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_EMBEDDED_VIDEO_PAGE_RE = re.compile(
    r"""(https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^"'`\s<>]*v=[A-Za-z0-9_-]{6,}|embed/[A-Za-z0-9_-]{6,}|shorts/[A-Za-z0-9_-]{6,})|youtu\.be/[A-Za-z0-9_-]{6,}|instagram\.com/(?:p|reel|reels|tv)/[A-Za-z0-9_-]{5,}|tiktok\.com/@[^"'`\s<>]+/video/\d+|(?:twitter|x)\.com/[^"'`\s<>]+/status/\d+|facebook\.com/(?:watch/\?v=\d+|[^"'`\s<>]+/videos/\d+)|reddit\.com/r/[^"'`\s<>]+/comments/[A-Za-z0-9_]+|(?:player\.)?vimeo\.com/(?:video/)?\d{5,}|dailymotion\.com/(?:embed/)?video/[A-Za-z0-9]+))""",
    re.IGNORECASE,
)
_INSTAGRAM_SHORTCODE_RE = re.compile(
    r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]{5,})",
    re.IGNORECASE,
)
_RAW_MEDIA_URL_RE = re.compile(
    r"""(https?:\\?/\\?/[^"'`\s<>\\]+?\.(?:mp4|m3u8|webm|m4v)(?:\?[^"'`\s<>]*)?)""",
    re.IGNORECASE,
)
_JSON_ESCAPED_MEDIA_RE = re.compile(
    r"""(https?:\\\/\\\/[^"'`\s<>]+?\.(?:mp4|m3u8|webm|m4v)(?:\\u0026[^"'`\s<>]*|\?[^"'`\s<>]*)?)""",
    re.IGNORECASE,
)
_CHALLENGE_MARKERS = (
    "cf-turnstile",
    "verify you are human",
    "g-recaptcha",
    "hcaptcha.com",
    "checking your browser before accessing",
)


@dataclass
class InspectionResult:
    kind: str  # "direct" | "videos"
    url: str
    probe: Optional[ProbeResult] = None
    videos: Optional[List[ExtractedVideo]] = None


def _app_root_dirs() -> List[str]:
    """Return candidate application root directories for both source and frozen .exe runs."""
    dirs: List[str] = []
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        dirs.append(exe_dir)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(os.path.abspath(meipass))
    src_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if src_root not in dirs:
        dirs.append(src_root)
    return dirs


def find_ffmpeg() -> Optional[str]:
    """Locate ffmpeg executable across portable .exe folders, data/bin, PATH, WinGet, Scoop, and Chocolatey."""
    exe_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"

    # 1. Check portable / bundled locations next to Happydownload.exe or inside data/bin
    for root in _app_root_dirs():
        for rel in (
            exe_name,
            os.path.join("bin", exe_name),
            os.path.join("data", "bin", exe_name),
            os.path.join("ffmpeg", exe_name),
            os.path.join("ffmpeg", "bin", exe_name),
        ):
            cand = os.path.join(root, rel)
            if os.path.isfile(cand):
                return cand

    # 2. Check system PATH
    found = shutil.which("ffmpeg")
    if found:
        return found

    # 3. Check standard Windows package managers (WinGet, Scoop, Chocolatey, C:\ffmpeg)
    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            link = os.path.join(local_app, "Microsoft", "WinGet", "Links", "ffmpeg.exe")
            if os.path.isfile(link):
                return link
            for pkg_pat in ("Gyan.FFmpeg*", "*FFmpeg*"):
                pattern = os.path.join(
                    local_app, "Microsoft", "WinGet", "Packages", pkg_pat, "*", "bin", "ffmpeg.exe"
                )
                matches = sorted(glob.glob(pattern), reverse=True)
                if matches:
                    return matches[0]

        user_profile = os.environ.get("USERPROFILE", "")
        if user_profile:
            for scoop_rel in (
                os.path.join("scoop", "shims", "ffmpeg.exe"),
                os.path.join("scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"),
            ):
                sc = os.path.join(user_profile, scoop_rel)
                if os.path.isfile(sc):
                    return sc

        for fixed in (
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        ):
            if os.path.isfile(fixed):
                return fixed
    return None


def install_ffmpeg_portable(target_bin_dir: Optional[str] = None) -> Tuple[bool, str, Optional[str]]:
    """Auto-install FFmpeg for end-users running the .exe on a PC without FFmpeg installed."""
    existing = find_ffmpeg()
    if existing:
        return True, f"FFmpeg is already available at {existing}", existing

    if sys.platform != "win32":
        return False, "Automatic FFmpeg installation is supported on Windows; please install ffmpeg via your package manager.", None

    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # Attempt 1: Silent WinGet install if winget is available on Windows 10/11
    if shutil.which("winget"):
        try:
            subprocess.run(
                [
                    "winget", "install", "--id", "Gyan.FFmpeg", "-e",
                    "--accept-source-agreements", "--accept-package-agreements", "--silent",
                ],
                capture_output=True,
                timeout=240,
                creationflags=no_win,
            )
            found = find_ffmpeg()
            if found:
                return True, f"Installed FFmpeg via Windows Package Manager ({found})", found
        except Exception:
            pass

    # Attempt 2: Download official portable Windows x64 ffmpeg build directly into <app_root>/data/bin/ffmpeg.exe
    base_root = _app_root_dirs()[0]
    bin_dir = target_bin_dir or os.path.join(base_root, "data", "bin")
    os.makedirs(bin_dir, exist_ok=True)
    dest_exe = os.path.join(bin_dir, "ffmpeg.exe")

    zip_urls = [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    ]
    last_err = "Could not reach FFmpeg mirror"
    for z_url in zip_urls:
        tmp_zip = None
        try:
            fd, tmp_zip = tempfile.mkstemp(suffix=".ffmpeg.zip", dir=bin_dir)
            os.close(fd)
            req = urllib.request.Request(
                z_url,
                headers={"User-Agent": "Happydownload/2.0 (Windows; Portable FFmpeg Provisioner)"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp_zip, "wb") as out_f:
                shutil.copyfileobj(resp, out_f, 1024 * 1024)

            with zipfile.ZipFile(tmp_zip, "r") as zf:
                for member in zf.namelist():
                    if member.lower().endswith("/bin/ffmpeg.exe") or member.lower() == "ffmpeg.exe":
                        with zf.open(member) as src, open(dest_exe, "wb") as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                        break

            if os.path.isfile(dest_exe) and os.path.getsize(dest_exe) > 1024 * 1024:
                return True, f"Downloaded portable FFmpeg to {dest_exe}", dest_exe
        except Exception as exc:
            last_err = str(exc)
        finally:
            if tmp_zip and os.path.exists(tmp_zip):
                try:
                    os.remove(tmp_zip)
                except OSError:
                    pass

    return False, f"FFmpeg auto-install failed ({last_err}). Place ffmpeg.exe next to Happydownload.exe.", None


def ensure_ffmpeg(auto_install: bool = True) -> Optional[str]:
    """Return FFmpeg path, optionally provisioning portable FFmpeg on-demand if missing."""
    path = find_ffmpeg()
    if path or not auto_install:
        return path
    ok, _, installed_path = install_ffmpeg_portable()
    return installed_path if ok else find_ffmpeg()


def get_capabilities() -> Dict[str, object]:
    ytdlp_version: Optional[str] = None
    try:
        import yt_dlp  # type: ignore

        ytdlp_version = getattr(yt_dlp.version, "__version__", "installed")
    except Exception:
        ytdlp_version = None

    ffmpeg_path = find_ffmpeg()
    return {
        "ytdlp_available": ytdlp_version is not None,
        "ytdlp_version": ytdlp_version,
        "ffmpeg_available": ffmpeg_path is not None,
        "ffmpeg_path": ffmpeg_path,
        "builtin_html_hls": True,
    }


def _looks_like_hls_url(url: str, content_type: Optional[str] = None) -> bool:
    if content_type:
        ct = content_type.lower()
        if "mpegurl" in ct or "m3u8" in ct:
            return True
    path = urlparse(url).path.lower()
    return path.endswith(".m3u8")


def _is_html_probe(probe: Optional[ProbeResult]) -> bool:
    if probe is None:
        return False
    ct = (probe.content_type or "").lower()
    return "text/html" in ct or "application/xhtml" in ct


def _looks_like_direct_binary(url: str, probe: ProbeResult) -> bool:
    ct = (probe.content_type or "").lower()
    cd = probe.headers.get("content-disposition", "").lower()
    if "attachment" in cd:
        return True
    if ct.startswith(("video/", "audio/")):
        return True
    if any(tok in ct for tok in ("application/octet-stream", "matroska", "x-zip", "x-rar", "x-iso")):
        return True
    if "text/html" in ct or "application/xhtml" in ct:
        return False
    path = urlparse(probe.final_url or url).path.lower()
    if any(path.endswith(ext) for ext in _BINARY_EXTS):
        return True
    return False


def _unescape_media_url(raw: str, base_url: str) -> str:
    cleaned = html.unescape(raw.strip())
    cleaned = cleaned.replace("\\/", "/").replace("\\u0026", "&").replace("\\u002F", "/")
    return urljoin(base_url, cleaned)


def _extract_title_from_html(html_text: str, fallback_url: str) -> str:
    m_og = _OG_TITLE_RE.search(html_text)
    if m_og:
        t = html.unescape(m_og.group(1)).strip()
        if t:
            return sanitize_filename(t, fallback="video")
    m_title = _TITLE_RE.search(html_text)
    if m_title:
        t = re.sub(r"\s+", " ", html.unescape(m_title.group(1))).strip()
        if t:
            return sanitize_filename(t, fallback="video")
    host = urlparse(fallback_url).hostname or "web_video"
    return sanitize_filename(host, fallback="web_video")


def _extract_hls_as_video(
    url: str,
    title: Optional[str],
    user_agent: str,
    custom_headers: Optional[Dict[str, str]],
) -> ExtractedVideo:
    final_url, text, _ = fetch_text_url(url, user_agent=user_agent, custom_headers=custom_headers)
    hls_info = parse_m3u8_text(final_url, text, headers_for_segments=custom_headers)
    if hls_info.is_master:
        formats = hls_info.variants
    else:
        formats = [
            ExtractedFormat(
                format_id="hls-default",
                url=final_url,
                ext="mp4" if hls_info.is_fmp4 else "ts",
                label="HLS Stream",
                protocol="hls",
                has_video=True,
                has_audio=True,
                http_headers=dict(custom_headers or {}),
            )
        ]
    return ExtractedVideo(
        id="hls-0",
        title=title or sanitize_filename(urlparse(final_url).path.rsplit("/", 1)[-1].replace(".m3u8", "") or "stream"),
        page_url=url,
        extractor="builtin-hls",
        duration=hls_info.total_duration or None,
        formats=formats,
    )


def _extract_from_html_fallback(
    url: str,
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
    depth: int = 0,
) -> List[ExtractedVideo]:
    final_url, body, resp_headers = fetch_text_url(url, user_agent=user_agent, custom_headers=custom_headers)
    ct = resp_headers.get("content-type", "").lower()
    if "mpegurl" in ct or body.lstrip().startswith("#EXTM3U"):
        return [_extract_hls_as_video(final_url, None, user_agent, custom_headers)]

    lower_body = body[:100000].lower()
    if any(marker in lower_body for marker in _CHALLENGE_MARKERS) and "<video" not in lower_body and ".m3u8" not in lower_body:
        raise FatalDownloadError("Target page is gated by a CAPTCHA/anti-bot check.")

    title = _extract_title_from_html(body, final_url)
    page_headers = dict(custom_headers or {})
    page_headers.setdefault("Referer", final_url)

    found_urls: List[str] = []
    seen = set()

    def add_candidate(raw_u: str) -> None:
        if not raw_u or raw_u.startswith(("data:", "blob:", "javascript:")):
            return
        abs_u = _unescape_media_url(raw_u, final_url)
        if abs_u.startswith(("http://", "https://")) and abs_u not in seen:
            seen.add(abs_u)
            found_urls.append(abs_u)

    for m in _META_VIDEO_RE.finditer(body):
        add_candidate(m.group(1))
    for m in _TAG_SRC_RE.finditer(body):
        add_candidate(m.group(1))
    for m in _JSON_ESCAPED_MEDIA_RE.finditer(body):
        add_candidate(m.group(1))
    for m in _RAW_MEDIA_URL_RE.finditer(body):
        add_candidate(m.group(1))

    formats: List[ExtractedFormat] = []
    for idx, media_url in enumerate(found_urls):
        path_lower = urlparse(media_url).path.lower()
        if ".m3u8" in path_lower or "m3u8" in media_url.lower():
            try:
                hls_vid = _extract_hls_as_video(media_url, title, user_agent, page_headers)
                formats.extend(hls_vid.formats)
                continue
            except Exception:
                pass
        ext = "mp4"
        for cand_ext in ("mkv", "webm", "m4v", "mp4"):
            if path_lower.endswith(f".{cand_ext}"):
                ext = cand_ext
                break
        res_m = re.search(r"(\d{3,4})p", media_url, re.IGNORECASE)
        height = int(res_m.group(1)) if res_m else None
        label = f"{height}p {ext.upper()}" if height else f"Direct {ext.upper()}"
        formats.append(
            ExtractedFormat(
                format_id=f"html-{idx}",
                url=media_url,
                ext=ext,
                label=label,
                protocol="http",
                height=height,
                has_video=True,
                has_audio=True,
                http_headers=dict(page_headers),
            )
        )

    videos: List[ExtractedVideo] = []
    if formats:
        formats.sort(key=lambda f: (1 if f.protocol == "http" else 0, f.height or 0), reverse=True)
        videos.append(
            ExtractedVideo(
                id="page-0",
                title=title,
                page_url=final_url,
                extractor="builtin-html",
                formats=formats,
            )
        )

    # Check embedded iframes, search-engine video wrappers (e.g. Bing Videos), and embedded external video host URLs
    if depth == 0 and not videos:
        embed_candidates: List[str] = []
        seen_embeds = set()

        def add_embed(raw_e: str) -> None:
            if not raw_e:
                return
            abs_e = _unescape_media_url(raw_e, final_url)
            if abs_e.startswith(("http://", "https://")) and abs_e != final_url and abs_e not in seen_embeds:
                seen_embeds.add(abs_e)
                embed_candidates.append(abs_e)

        for unwrapped in _unwrap_search_engine_video_page(final_url, body, user_agent, page_headers):
            add_embed(unwrapped)
        for m in _IFRAME_SRC_RE.finditer(body):
            add_embed(m.group(1))
        for m in _EMBEDDED_VIDEO_PAGE_RE.finditer(html.unescape(body)):
            add_embed(m.group(1))

        for embed_url in embed_candidates[:6]:
            sub_videos = _extract_with_ytdlp(embed_url, custom_headers=page_headers)
            if not sub_videos:
                try:
                    sub_videos = _extract_from_html_fallback(
                        embed_url, user_agent=user_agent, custom_headers=page_headers, depth=1
                    )
                except Exception:
                    continue
            for sv in sub_videos:
                if sv.title == sanitize_filename(urlparse(embed_url).hostname or ""):
                    sv.title = title
                videos.append(sv)
            if videos:
                break

    return videos


_VRHM_ATTR_RE = re.compile(r'vrhm=["\']([^"\']+)["\']', re.IGNORECASE)
_PAGECONFIG_RE = re.compile(r'data-pageconfig=["\']([^"\']+)["\']', re.IGNORECASE)


def _unwrap_search_engine_video_page(
    url: str,
    body: str,
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Unwrap Bing/Google/Yahoo/DuckDuckGo video wrapper URLs into their actual source video URLs."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    qs = parse_qs(parsed.query)
    candidates: List[str] = []
    seen = set()

    def push(u: Optional[str]) -> None:
        if not u or not isinstance(u, str):
            return
        cleaned = unquote(html.unescape(u.strip()))
        if cleaned.startswith(("http://", "https://")) and cleaned != url and cleaned not in seen:
            # Ignore bare channel URLs without a specific video
            if "/channel/" in cleaned and "watch?v=" not in cleaned:
                return
            seen.add(cleaned)
            candidates.append(cleaned)

    # 1. Direct query parameter redirect targets (Google, Yahoo, etc.)
    for key in ("murl", "purl", "mediaurl", "url", "u", "ru", "target"):
        for val in qs.get(key, []):
            if val.startswith(("http://", "https://", "http%3A", "https%3A")):
                push(val)

    # 1.5 Instagram public embed fallback (/p/<shortcode>/embed/captioned/) when direct page requires login
    m_ig = _INSTAGRAM_SHORTCODE_RE.search(url)
    if m_ig and not candidates:
        shortcode = m_ig.group(1)
        ig_embed = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
        if ig_embed != url:
            push(ig_embed)

    # 2. Bing Videos `vrhm` metadata cards embedded in HTML
    target_mid = (qs.get("mid") or [None])[0]
    vrhm_fallback: List[str] = []
    for m in _VRHM_ATTR_RE.finditer(body):
        try:
            card = json.loads(html.unescape(m.group(1)))
            murl = card.get("murl") or card.get("purl")
            if not murl:
                continue
            if target_mid and str(card.get("mid") or "").upper() == str(target_mid).upper():
                push(murl)
            else:
                vrhm_fallback.append(murl)
        except Exception:
            continue
    if not candidates:
        for u in vrhm_fallback[:3]:
            push(u)

    # 3. Bing Videos `relatedvideo` / `detail` dynamic wrapper resolution by `mid`
    if "bing.com" in host and target_mid and not candidates:
        # 3a. Query Bing video search directly by `mid` to inspect SSR `vrhm` cards
        try:
            search_u = f"https://www.bing.com/videos/search?q={target_mid}"
            _, s_body, _ = fetch_text_url(search_u, user_agent=user_agent, custom_headers=custom_headers)
            for m in _VRHM_ATTR_RE.finditer(s_body):
                try:
                    card = json.loads(html.unescape(m.group(1)))
                    murl = card.get("murl") or card.get("purl")
                    if murl and str(card.get("mid") or "").upper() == str(target_mid).upper():
                        push(murl)
                    elif murl:
                        vrhm_fallback.append(murl)
                except Exception:
                    continue
            for u in vrhm_fallback[:2]:
                push(u)
        except Exception:
            pass

        # 3b. Query Bing `cusvdp` JSON details API if configured in `data-pageconfig`
        if not candidates:
            m_cfg = _PAGECONFIG_RE.search(body)
            if m_cfg:
                try:
                    cfg = json.loads(html.unescape(m_cfg.group(1)))
                    cus = ((cfg.get("acm") or {}).get("cusvdp")) or {}
                    auf = cus.get("auf")
                    pqs = cus.get("persistedQueryStrings") or ""
                    icl = cus.get("icl") or []
                    pm_dict = icl[0].get("pm") if icl and isinstance(icl[0], dict) else {"modules": "videoresult,queryresultvideos"}
                    if auf:
                        pm_str = urlencode(pm_dict)
                        api_url = f"https://www.bing.com{auf}{pm_str}&{parsed.query}&{pqs}"
                        _, api_txt, _ = fetch_text_url(api_url, user_agent=user_agent, custom_headers=custom_headers)
                        api_data = json.loads(api_txt)
                        for section_key in ("videoByIdResults", "videoVerticalQueryResults"):
                            sec = api_data.get(section_key)
                            items = sec.get("value") if isinstance(sec, dict) else (sec if isinstance(sec, list) else [])
                            for item in (items or []):
                                if isinstance(item, dict):
                                    vid_u = item.get("contentUrl") or item.get("hostPageUrl")
                                    if str(item.get("videoId") or "").upper() == str(target_mid).upper():
                                        push(vid_u)
                                    elif vid_u:
                                        vrhm_fallback.append(vid_u)
                        for u in vrhm_fallback[:2]:
                            push(u)
                except Exception:
                    pass

    return candidates


def _convert_ytdlp_entry(entry: dict, page_url: str, ffmpeg_available: bool) -> Optional[ExtractedVideo]:
    if not entry:
        return None
    title = sanitize_filename(entry.get("title") or "video", fallback="video")
    raw_formats = entry.get("formats") or []
    # Single-URL flat or direct entry fallback
    if not raw_formats and entry.get("url"):
        raw_formats = [entry]

    # When FFmpeg is available, locate the highest-quality audio-only stream so 1080p/2K/4K
    # video-only DASH formats can be downloaded in parallel and losslessly merged with audio.
    best_audio: Optional[Dict[str, object]] = None
    if ffmpeg_available:
        audio_candidates: List[Dict[str, object]] = []
        for f in raw_formats:
            f_url = f.get("url")
            if not f_url:
                continue
            proto = (f.get("protocol") or "https").lower()
            if proto not in ("http", "https", "m3u8", "m3u8_native"):
                continue
            if "drm" in str(f.get("has_drm") or "").lower() or f.get("has_drm") is True:
                continue
            vcodec = (f.get("vcodec") or "").lower()
            acodec = (f.get("acodec") or "").lower()
            if vcodec == "none" and acodec not in ("none", ""):
                ext = (f.get("ext") or "m4a").lower()
                abr = float(f.get("abr") or f.get("tbr") or 0.0)
                fsize = f.get("filesize") or f.get("filesize_approx")
                audio_candidates.append(
                    {
                        "url": str(f_url),
                        "ext": ext,
                        "norm_proto": "http" if proto in ("http", "https") else "hls",
                        "abr": abr,
                        "filesize": int(fsize) if fsize else 0,
                        "hdrs": {str(k): str(v) for k, v in (f.get("http_headers") or {}).items()},
                        "is_m4a": 1 if ext in ("m4a", "aac", "mp4") else 0,
                    }
                )
        if audio_candidates:
            audio_candidates.sort(
                key=lambda a: (
                    1 if a["norm_proto"] == "http" else 0,
                    int(a["is_m4a"]),
                    float(a["abr"]),
                ),
                reverse=True,
            )
            best_audio = audio_candidates[0]

    extracted_formats: List[ExtractedFormat] = []
    seen_urls = set()

    for f in raw_formats:
        f_url = f.get("url")
        if not f_url or f_url in seen_urls:
            continue
        proto = (f.get("protocol") or "https").lower()
        if "drm" in str(f.get("has_drm") or "").lower() or f.get("has_drm") is True:
            continue
        vcodec = (f.get("vcodec") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        ext = (f.get("ext") or "mp4").lower()
        if ext == "mhtml" or (vcodec == "none" and acodec == "none"):
            continue

        has_video = vcodec != "none"
        has_audio = acodec != "none"
        paired_audio: Optional[Dict[str, object]] = None

        if has_video and not has_audio:
            if ffmpeg_available and best_audio is not None and best_audio["norm_proto"] == "http" and proto in ("http", "https"):
                paired_audio = best_audio
                has_audio = True
                ext = "mp4"
            else:
                continue

        # Only use protocols Happydownload's HTTP/HLS engine natively downloads cleanly
        if proto in ("http", "https"):
            norm_proto = "http"
        elif proto in ("m3u8", "m3u8_native"):
            norm_proto = "hls"
            if ext == "mp4":
                ext = "ts"
        else:
            continue

        seen_urls.add(f_url)
        height = f.get("height")
        if not height:
            hint_str = " ".join(
                str(x or "") for x in (f.get("format_id"), f.get("format_note"), f_url)
            )
            m_h = re.search(r"(\d{3,4})p", hint_str, re.IGNORECASE)
            if m_h:
                cand_h = int(m_h.group(1))
                if 144 <= cand_h <= 4320:
                    height = cand_h
        tbr = f.get("tbr")
        fsize = f.get("filesize") or f.get("filesize_approx")
        if paired_audio and fsize and paired_audio.get("filesize"):
            fsize = int(fsize) + int(paired_audio["filesize"])

        parts = []
        if height:
            res_tag = f"{height}p"
            if int(height) >= 2160:
                res_tag += " 4K"
            elif int(height) >= 1440:
                res_tag += " 2K"
            elif int(height) >= 1080:
                res_tag += " HD"
            parts.append(res_tag)
        elif not has_video and has_audio:
            parts.append("Audio Only")
        else:
            parts.append(f.get("format_note") or "Stream")
        parts.append(ext.upper())
        if tbr:
            parts.append(f"{int(tbr)} kbps")
        if paired_audio:
            parts.append("HD+Audio Merge")
        elif norm_proto == "hls":
            parts.append("HLS")

        hdrs = {str(k): str(v) for k, v in (f.get("http_headers") or {}).items()}
        extracted_formats.append(
            ExtractedFormat(
                format_id=str(f.get("format_id") or len(extracted_formats)),
                url=f_url,
                ext=ext,
                label=" • ".join(parts),
                protocol=norm_proto,
                height=int(height) if height else None,
                tbr_kbps=float(tbr) if tbr else None,
                filesize_approx=int(fsize) if fsize else None,
                has_video=has_video,
                has_audio=has_audio,
                http_headers=hdrs,
                audio_url=str(paired_audio["url"]) if paired_audio else None,
                audio_ext=str(paired_audio["ext"]) if paired_audio else None,
                audio_protocol=str(paired_audio["norm_proto"]) if paired_audio else "http",
                audio_headers=dict(paired_audio["hdrs"]) if paired_audio else {},  # type: ignore[arg-type]
            )
        )

    if not extracted_formats:
        return None

    # Sort best combined/merged video+audio highest resolution first, preferring direct progressive over split merge at identical resolution
    extracted_formats.sort(
        key=lambda fmt: (
            1 if (fmt.has_video and fmt.has_audio) else 0,
            1 if fmt.protocol == "http" else 0,
            fmt.height or 0,
            0 if fmt.audio_url else 1,
            1 if fmt.ext == "mp4" else 0,
            fmt.tbr_kbps or 0.0,
        ),
        reverse=True,
    )

    # Deduplicate near-identical resolution tiers so the UI dropdown stays clean and readable
    deduped: List[ExtractedFormat] = []
    seen_tiers = set()
    for fmt in extracted_formats:
        tier_key = (fmt.has_video, fmt.has_audio, fmt.height or 0, fmt.protocol, bool(fmt.audio_url))
        if fmt.height and tier_key in seen_tiers:
            continue
        seen_tiers.add(tier_key)
        deduped.append(fmt)

    return ExtractedVideo(
        id=str(entry.get("id") or "video"),
        title=title,
        page_url=entry.get("webpage_url") or page_url,
        extractor=str(entry.get("extractor_key") or entry.get("extractor") or "yt-dlp"),
        duration=float(entry["duration"]) if entry.get("duration") else None,
        thumbnail=entry.get("thumbnail"),
        formats=deduped,
    )


class _SilentYtdlpLogger:
    """Suppress noisy yt-dlp stderr prints while capturing fatal error reasons."""

    def __init__(self) -> None:
        self.errors: List[str] = []

    def debug(self, msg: str) -> None:
        return

    def info(self, msg: str) -> None:
        return

    def warning(self, msg: str) -> None:
        return

    def error(self, msg: str) -> None:
        if msg:
            self.errors.append(str(msg))


def _extract_with_ytdlp(
    url: str,
    custom_headers: Optional[Dict[str, str]] = None,
    cookies_from_browser: Optional[str] = None,
) -> List[ExtractedVideo]:
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return []

    ffmpeg_path = find_ffmpeg()
    ffmpeg_ok = ffmpeg_path is not None

    def _run_ydl(use_browser_cookies: Optional[str]) -> Tuple[Optional[dict], List[str]]:
        logger = _SilentYtdlpLogger()
        opts: Dict[str, object] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": logger,
            "skip_download": True,
            "playlistend": 30,
            "ignoreerrors": True,
            "socket_timeout": 15,
        }
        if ffmpeg_path:
            opts["ffmpeg_location"] = ffmpeg_path
        if custom_headers:
            opts["http_headers"] = dict(custom_headers)
        if use_browser_cookies and use_browser_cookies not in ("none", ""):
            opts["cookiesfrombrowser"] = (use_browser_cookies,)

        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                res_info = ydl.extract_info(url, download=False)
                return res_info, logger.errors
            except Exception as exc:
                logger.errors.append(str(exc))
                return None, logger.errors

    browser_pref = (cookies_from_browser or "none").strip().lower()
    info, errors = _run_ydl(browser_pref if browser_pref != "none" else None)

    # If cookiesfrombrowser failed due to locked browser profile DB, retry cleanly without cookies
    if not info and browser_pref != "none":
        info, errors = _run_ydl(None)

    if not info:
        err_text = " ".join(errors).lower()
        if any(k in err_text for k in ("drm", "widevine", "fairplay", "protected")):
            raise FatalDownloadError("This video is DRM-protected and cannot be downloaded.")
        return []

    if info.get("_type") == "playlist" and info.get("entries"):
        videos: List[ExtractedVideo] = []
        for entry in info["entries"]:
            vid = _convert_ytdlp_entry(entry, url, ffmpeg_ok)
            if vid:
                videos.append(vid)
        return videos

    vid = _convert_ytdlp_entry(info, url, ffmpeg_ok)
    return [vid] if vid else []


def inspect_any_url(
    url: str,
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
    cookies_from_browser: Optional[str] = None,
) -> InspectionResult:
    """Inspect a direct file link, HLS playlist, or web video page (1 or multiple videos)."""
    if _looks_like_hls_url(url):
        vid = _extract_hls_as_video(url, None, user_agent=user_agent, custom_headers=custom_headers)
        return InspectionResult(kind="videos", url=url, videos=[vid])

    probe: Optional[ProbeResult] = None
    probe_err: Optional[Exception] = None
    try:
        probe = probe_url(url, user_agent=user_agent, custom_headers=custom_headers, timeout=timeout)
    except Exception as exc:
        probe_err = exc

    if probe is not None:
        if _looks_like_hls_url(probe.final_url, probe.content_type):
            vid = _extract_hls_as_video(probe.final_url, None, user_agent=user_agent, custom_headers=custom_headers)
            return InspectionResult(kind="videos", url=url, videos=[vid])

        if _looks_like_direct_binary(url, probe):
            return InspectionResult(kind="direct", url=url, probe=probe)

    # Not a direct binary file (likely an HTML video page, or site rejected raw HEAD/Range)
    videos = _extract_with_ytdlp(url, custom_headers=custom_headers, cookies_from_browser=cookies_from_browser)
    if not videos:
        try:
            videos = _extract_from_html_fallback(url, user_agent=user_agent, custom_headers=custom_headers)
        except FatalDownloadError:
            raise
        except Exception as exc:
            if probe is not None and probe_err is None and not _is_html_probe(probe):
                return InspectionResult(kind="direct", url=url, probe=probe)
            raise FatalDownloadError(f"Could not extract video from page: {exc}") from exc

    if videos:
        return InspectionResult(kind="videos", url=url, videos=videos)

    if probe is not None and not _is_html_probe(probe):
        return InspectionResult(kind="direct", url=url, probe=probe)

    if _is_html_probe(probe):
        host = (urlparse(probe.final_url or url).hostname or "").lower()
        path_q = (probe.final_url or url).lower()
        if any(s in host for s in ("bing.com", "google.", "duckduckgo.com", "search.yahoo.com")) and (
            "/videos" in path_q or "/search" in path_q or "relatedvideo" in path_q
        ):
            raise FatalDownloadError(
                "This is a search engine video wrapper page whose player loads dynamically via JavaScript. "
                "Open the video on its source site (e.g., YouTube) and paste that direct video URL instead."
            )
        raise FatalDownloadError(
            "No downloadable video or media stream was found on this web page (it may load dynamically via JavaScript)."
        )

    if probe_err is not None:
        raise probe_err
    raise FatalDownloadError("No downloadable videos or media streams found on that URL.")
