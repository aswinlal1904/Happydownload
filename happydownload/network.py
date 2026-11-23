"""HTTP/HTTPS networking, URL probing, keep-alive pooling, redirect resolution, and error classification."""

from __future__ import annotations

from dataclasses import dataclass
import http.client
import os
import re
import socket
import ssl
from typing import Dict, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

from .models import ProbeResult

REDIRECT_CODES = {301, 302, 303, 307, 308}
THROTTLE_CODES = {429, 503}
TRANSIENT_CODES = {408, 500, 502, 504, 520, 521, 522, 523, 524}
LINK_EXPIRED_CODES = {401, 403, 410}

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DownloadError(Exception):
    """Base error for download network operations."""


class TransientNetworkError(DownloadError):
    """Retryable network or temporary server error."""


class ThrottleError(DownloadError):
    """Server is rate-limiting or overloaded (429 / 503)."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LinkExpiredError(DownloadError):
    """Signed/redirected URL may have expired and needs re-probing."""


class FatalDownloadError(DownloadError):
    """Non-recoverable error (e.g., 404, invalid URL, remote file changed)."""


@dataclass
class HttpStream:
    """Active streaming HTTP response wrapper with optional keep-alive connection return."""

    conn: http.client.HTTPConnection
    resp: http.client.HTTPResponse
    status: int
    headers: Dict[str, str]
    final_url: str
    pool_key: Optional[Tuple[str, str, int]] = None
    pool: Optional["KeepAliveClient"] = None

    def read(self, size: int) -> bytes:
        return self.resp.read(size)

    def close(self, force_close: bool = False) -> None:
        can_reuse = (
            not force_close
            and self.pool is not None
            and self.pool_key is not None
            and self.headers.get("connection", "").lower() != "close"
            and not getattr(self.resp, "will_close", True)
            and self.resp.isclosed()
        )
        try:
            self.resp.close()
        except Exception:
            can_reuse = False

        if can_reuse and self.pool is not None and self.pool_key is not None:
            self.pool._return_connection(self.pool_key, self.conn)
        else:
            try:
                self.conn.close()
            except Exception:
                pass


def sanitize_filename(name: str, fallback: str = "download.bin") -> str:
    """Sanitize a filename safely for Windows and POSIX filesystems."""
    cleaned = unquote(name).strip().strip(".")
    cleaned = os.path.basename(cleaned)
    cleaned = _INVALID_FILENAME_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return fallback
    stem = os.path.splitext(cleaned)[0].upper()
    if stem in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    return cleaned[:220]


def parse_content_disposition(header_val: Optional[str]) -> Optional[str]:
    """Extract filename from Content-Disposition header (RFC 5987 / RFC 6266)."""
    if not header_val:
        return None
    m_ext = re.search(r"filename\*\s*=\s*(?:[^']*'[^']*'|utf-8'')([^;\r\n]+)", header_val, re.IGNORECASE)
    if m_ext:
        return sanitize_filename(m_ext.group(1).strip().strip('"'))
    m_basic = re.search(r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\r\n]+)', header_val, re.IGNORECASE)
    if m_basic:
        raw = m_basic.group(1) or m_basic.group(2)
        return sanitize_filename(raw.strip())
    return None


def filename_from_url(url: str) -> str:
    """Derive a clean filename from a URL path."""
    parsed = urlparse(url)
    base = os.path.basename(parsed.path.rstrip("/"))
    if base:
        return sanitize_filename(base)
    return "movie_download.bin"


def _conn_key(parsed_url) -> Tuple[str, str, int]:
    scheme = parsed_url.scheme.lower()
    host = parsed_url.hostname or ""
    port = parsed_url.port or (443 if scheme == "https" else 80)
    return (scheme, host, port)


def _make_connection(
    parsed_url,
    timeout: float,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> http.client.HTTPConnection:
    scheme = parsed_url.scheme.lower()
    host = parsed_url.hostname
    if not host:
        raise FatalDownloadError(f"Invalid URL host: {parsed_url.geturl()}")
    if scheme == "https":
        ctx = ssl_context or ssl.create_default_context()
        port = parsed_url.port or 443
        return http.client.HTTPSConnection(host, port=port, timeout=timeout, context=ctx)
    if scheme == "http":
        port = parsed_url.port or 80
        return http.client.HTTPConnection(host, port=port, timeout=timeout)
    raise FatalDownloadError(f"Unsupported URL scheme: {scheme}")


def _configure_socket(conn: http.client.HTTPConnection) -> None:
    """Optimize TCP socket buffer & latency for large high-throughput transfers."""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
    except OSError:
        pass


def _parse_retry_after(headers: Dict[str, str]) -> Optional[float]:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(1.0, float(raw.strip()))
    except ValueError:
        return None


class KeepAliveClient:
    """Per-worker HTTP/HTTPS connection pool reusing sockets across consecutive segments."""

    def __init__(self) -> None:
        self._conn: Optional[http.client.HTTPConnection] = None
        self._key: Optional[Tuple[str, str, int]] = None
        self.reused_count: int = 0

    def _borrow_connection(
        self,
        parsed_url,
        timeout: float,
    ) -> Tuple[http.client.HTTPConnection, Tuple[str, str, int], bool]:
        key = _conn_key(parsed_url)
        if self._conn is not None and self._key == key and self._conn.sock is not None:
            conn = self._conn
            self._conn = None
            self._key = None
            conn.timeout = timeout
            try:
                conn.sock.settimeout(timeout)
            except OSError:
                try:
                    conn.close()
                except Exception:
                    pass
                return _make_connection(parsed_url, timeout), key, False
            return conn, key, True

        self.close()
        return _make_connection(parsed_url, timeout), key, False

    def _return_connection(self, key: Tuple[str, str, int], conn: http.client.HTTPConnection) -> None:
        if conn.sock is None:
            return
        if self._conn is not None and self._conn is not conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = conn
        self._key = key

    def open_request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        timeout: float,
        max_redirects: int = 10,
    ) -> HttpStream:
        """Execute an HTTP/HTTPS request reusing keep-alive sockets when possible."""
        current_url = url
        for _ in range(max_redirects + 1):
            parsed = urlparse(current_url)
            if parsed.scheme.lower() not in ("http", "https"):
                raise FatalDownloadError(f"Unsupported URL scheme: {parsed.scheme}")
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            resp = None
            conn = None
            key = _conn_key(parsed)
            # Retry once transparently if a pooled idle keep-alive socket was closed by server
            for attempt in range(2):
                conn, key, was_reused = self._borrow_connection(parsed, timeout)
                try:
                    if not was_reused:
                        conn.connect()
                        _configure_socket(conn)
                    conn.request(method, path, headers=headers)
                    resp = conn.getresponse()
                    if was_reused:
                        self.reused_count += 1
                    break
                except (
                    http.client.RemoteDisconnected,
                    http.client.BadStatusLine,
                    ConnectionResetError,
                    BrokenPipeError,
                    ConnectionAbortedError,
                    OSError,
                    ssl.SSLError,
                ) as exc:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if was_reused and attempt == 0:
                        continue
                    raise TransientNetworkError(str(exc)) from exc
                except (socket.timeout, TimeoutError, http.client.HTTPException) as exc:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise TransientNetworkError(str(exc)) from exc

            assert conn is not None and resp is not None
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            if resp.status in REDIRECT_CODES:
                location = resp_headers.get("location")
                try:
                    resp.read()
                    resp.close()
                    if resp_headers.get("connection", "").lower() != "close" and not getattr(resp, "will_close", True):
                        self._return_connection(key, conn)
                    else:
                        conn.close()
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                if not location:
                    raise FatalDownloadError(f"Redirect ({resp.status}) missing Location header")
                current_url = urljoin(current_url, location)
                if resp.status == 303 and method != "HEAD":
                    method = "GET"
                continue

            if resp.status in THROTTLE_CODES:
                retry_after = _parse_retry_after(resp_headers)
                resp.close()
                conn.close()
                raise ThrottleError(f"HTTP {resp.status} rate limit/service busy", retry_after=retry_after)

            if resp.status in TRANSIENT_CODES:
                resp.close()
                conn.close()
                raise TransientNetworkError(f"HTTP {resp.status} temporary server error")

            if resp.status in LINK_EXPIRED_CODES:
                resp.close()
                conn.close()
                raise LinkExpiredError(f"HTTP {resp.status} authorization/link expired")

            if resp.status == 404 or resp.status == 416 or resp.status >= 400:
                status_code = resp.status
                reason = resp.reason
                resp.close()
                conn.close()
                raise FatalDownloadError(f"HTTP {status_code} {reason}")

            return HttpStream(
                conn=conn,
                resp=resp,
                status=resp.status,
                headers=resp_headers,
                final_url=current_url,
                pool_key=key,
                pool=self,
            )

        raise FatalDownloadError("Too many HTTP redirects")

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._key = None


def build_base_headers(
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
    keep_alive: bool = False,
) -> Dict[str, str]:
    headers = {
        "User-Agent": user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Connection": "keep-alive" if keep_alive else "close",
    }
    if custom_headers:
        for k, v in custom_headers.items():
            if k and v is not None:
                headers[str(k)] = str(v)
    return headers


def _parse_content_range_total(header_val: Optional[str]) -> Optional[int]:
    """Parse total byte size from `Content-Range: bytes 0-0/53687091200`."""
    if not header_val:
        return None
    m = re.search(r"bytes\s+\d+-\d+/(\d+|\*)", header_val, re.IGNORECASE)
    if m and m.group(1) != "*":
        return int(m.group(1))
    return None


def probe_url(
    url: str,
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
    timeout: float = 15.0,
) -> ProbeResult:
    """Probe URL via HEAD then fallback to GET Range=bytes=0-0 to learn size and range support."""
    base_headers = build_base_headers(user_agent, custom_headers, keep_alive=False)
    client = KeepAliveClient()

    head_result: Optional[ProbeResult] = None
    try:
        stream = client.open_request("HEAD", url, headers=base_headers, timeout=timeout)
        try:
            hdrs = stream.headers
            cl = hdrs.get("content-length")
            total_bytes = int(cl) if cl and cl.isdigit() else None
            accept_ranges = "bytes" in hdrs.get("accept-ranges", "").lower()
            filename = (
                parse_content_disposition(hdrs.get("content-disposition"))
                or filename_from_url(stream.final_url)
            )
            head_result = ProbeResult(
                original_url=url,
                final_url=stream.final_url,
                filename=filename,
                total_bytes=total_bytes,
                accept_ranges=accept_ranges,
                etag=hdrs.get("etag"),
                last_modified=hdrs.get("last-modified"),
                content_type=hdrs.get("content-type"),
                status_code=stream.status,
                headers=hdrs,
            )
        finally:
            stream.close(force_close=True)
    except (FatalDownloadError, LinkExpiredError):
        head_result = None

    if head_result and head_result.total_bytes is not None and head_result.accept_ranges:
        client.close()
        return head_result

    range_headers = dict(base_headers)
    range_headers["Range"] = "bytes=0-0"
    stream = client.open_request("GET", url, headers=range_headers, timeout=timeout)
    try:
        hdrs = stream.headers
        filename = (
            parse_content_disposition(hdrs.get("content-disposition"))
            or (head_result.filename if head_result else None)
            or filename_from_url(stream.final_url)
        )
        if stream.status == 206:
            total_bytes = _parse_content_range_total(hdrs.get("content-range"))
            if total_bytes is None and head_result:
                total_bytes = head_result.total_bytes
            return ProbeResult(
                original_url=url,
                final_url=stream.final_url,
                filename=filename,
                total_bytes=total_bytes,
                accept_ranges=True,
                etag=hdrs.get("etag") or (head_result.etag if head_result else None),
                last_modified=hdrs.get("last-modified") or (head_result.last_modified if head_result else None),
                content_type=hdrs.get("content-type"),
                status_code=206,
                headers=hdrs,
            )

        cl = hdrs.get("content-length")
        total_bytes = int(cl) if cl and cl.isdigit() else (head_result.total_bytes if head_result else None)
        return ProbeResult(
            original_url=url,
            final_url=stream.final_url,
            filename=filename,
            total_bytes=total_bytes,
            accept_ranges=False,
            etag=hdrs.get("etag") or (head_result.etag if head_result else None),
            last_modified=hdrs.get("last-modified") or (head_result.last_modified if head_result else None),
            content_type=hdrs.get("content-type"),
            status_code=stream.status,
            headers=hdrs,
        )
    finally:
        stream.close(force_close=True)
        client.close()


def open_range_stream(
    url: str,
    start_byte: int,
    end_byte: Optional[int],
    user_agent: str,
    custom_headers: Optional[Dict[str, str]] = None,
    timeout: float = 20.0,
    client: Optional[KeepAliveClient] = None,
) -> HttpStream:
    """Open a streaming GET request for `[start_byte, end_byte]`, reusing keep-alive if client provided."""
    use_keep_alive = client is not None
    headers = build_base_headers(user_agent, custom_headers, keep_alive=use_keep_alive)
    if end_byte is not None and end_byte >= start_byte:
        headers["Range"] = f"bytes={start_byte}-{end_byte}"
    elif start_byte > 0:
        headers["Range"] = f"bytes={start_byte}-"

    pool = client or KeepAliveClient()
    stream = pool.open_request("GET", url, headers=headers, timeout=timeout)
    if client is None:
        stream.pool = None
    if end_byte is not None and start_byte > 0 and stream.status == 200:
        stream.close(force_close=True)
        raise FatalDownloadError("Server ignored Range header during segmented download")
    return stream
