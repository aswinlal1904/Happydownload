"""Local HTTP API, Server-Sent Events telemetry stream, and static Web UI server."""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from . import __version__
from .engine import DownloadManager
from .extractor import get_capabilities, install_ffmpeg_portable
from .network import DownloadError
from .storage import get_filesystem_name


def _resolve_static_dir() -> str:
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    if os.path.isdir(local):
        return local
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cand = os.path.join(meipass, "happydownload", "static")
        if os.path.isdir(cand):
            return cand
    return local


STATIC_DIR = _resolve_static_dir()


def _disk_info(path: str) -> Dict[str, Any]:
    target = os.path.abspath(path)
    probe_dir = target
    while probe_dir and not os.path.exists(probe_dir):
        parent = os.path.dirname(probe_dir)
        if parent == probe_dir:
            break
        probe_dir = parent
    try:
        usage = shutil.disk_usage(probe_dir or ".")
        return {
            "path": target,
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "filesystem": get_filesystem_name(probe_dir or "."),
        }
    except OSError:
        return {"path": target, "free_bytes": 0, "total_bytes": 0, "filesystem": None}


def _pick_folder_native(initial_dir: str) -> Optional[str]:
    """Open native folder picker in an isolated OS dialog (works in both Python and frozen .exe)."""
    if sys.platform == "win32":
        safe_init = (initial_dir or "").replace("'", "''")
        ps_code = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$f.Description = 'Select Download Folder'; "
            "$f.ShowNewFolderButton = $true; "
            f"if (Test-Path '{safe_init}') {{ $f.SelectedPath = '{safe_init}' }}; "
            "$top = New-Object System.Windows.Forms.Form; $top.TopMost = $true; "
            "if ($f.ShowDialog($top) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath }"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", ps_code],
                capture_output=True,
                text=True,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            chosen = proc.stdout.strip()
            return chosen if chosen else None
        except Exception:
            return None

    code = (
        "import tkinter as tk; from tkinter import filedialog; "
        "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
        f"p = filedialog.askdirectory(initialdir={initial_dir!r}, title='Select Download Folder'); "
        "print(p or '')"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        chosen = proc.stdout.strip()
        return chosen if chosen else None
    except Exception:
        return None


def _open_in_explorer(dest_path: str, dest_dir: str) -> bool:
    try:
        if sys.platform == "win32":
            if os.path.exists(dest_path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(dest_path)])
            elif os.path.exists(dest_dir):
                os.startfile(dest_dir)
            return True
        elif sys.platform == "darwin":
            subprocess.Popen(["open", dest_dir])
            return True
        else:
            subprocess.Popen(["xdg-open", dest_dir])
            return True
    except Exception:
        return False


class HappydownloadHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass, manager: DownloadManager) -> None:
        super().__init__(server_address, RequestHandlerClass)
        self.manager = manager

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        if isinstance(exc, OSError) and (
            getattr(exc, "winerror", None) in (10053, 10054, 10058)
            or getattr(exc, "errno", None) in (32, 104)
        ):
            return
        super().handle_error(request, client_address)


class HappydownloadHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        # Keep console output clean
        return

    @property
    def manager(self) -> DownloadManager:
        return self.server.manager  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1024 * 1024))
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _serve_static(self, rel_path: str) -> None:
        if rel_path in ("", "/"):
            rel_path = "index.html"
        rel_path = rel_path.lstrip("/")
        safe_path = os.path.abspath(os.path.join(STATIC_DIR, rel_path))
        if not safe_path.startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(safe_path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        ctype, _ = mimetypes.guess_type(safe_path)
        if safe_path.lower().endswith(".ico"):
            ctype = "image/x-icon"
        elif safe_path.lower().endswith(".png"):
            ctype = "image/png"
        with open(safe_path, "rb") as f:
            data = f.read()
        content_type = ctype or "application/octet-stream"
        if content_type.startswith("text/") or "javascript" in content_type or "json" in content_type:
            content_type = f"{content_type}; charset=utf-8"
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/info":
            from . import __author__

            self._send_json(
                HTTPStatus.OK,
                {
                    "version": __version__,
                    "author": __author__,
                    "default_download_dir": self.manager.default_download_dir,
                    "disk": _disk_info(self.manager.default_download_dir),
                    "settings": self.manager.get_settings(),
                    "orphans": self.manager.scan_orphans(),
                    "capabilities": get_capabilities(),
                },
            )
            return

        if path == "/api/settings":
            self._send_json(HTTPStatus.OK, {"settings": self.manager.get_settings()})
            return

        if path == "/api/orphans":
            self._send_json(HTTPStatus.OK, {"orphans": self.manager.scan_orphans()})
            return

        if path == "/api/downloads":
            tasks = self.manager.list_tasks()
            total_speed = sum(float(t["speed_bps"]) for t in tasks)
            self._send_json(
                HTTPStatus.OK,
                {
                    "downloads": tasks,
                    "total_speed_bps": total_speed,
                    "disk": _disk_info(self.manager.default_download_dir),
                    "settings": self.manager.get_settings(),
                    "orphans": self.manager.scan_orphans(),
                    "clipboard_candidate": self.manager.get_clipboard_candidate(),
                    "recent_completions": self.manager.get_recent_completions(),
                },
            )
            return

        if path == "/api/events":
            self._handle_sse()
            return

        self._serve_static(path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/probe":
                body = self._read_json_body()
                url = (body.get("url") or "").strip()
                if not url:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "URL is required"})
                    return
                res = self.manager.probe(
                    url=url,
                    dest_dir=body.get("dest_dir") or None,
                    custom_headers=body.get("custom_headers") or None,
                )
                self._send_json(HTTPStatus.OK, res)
                return

            if path == "/api/pick-folder":
                body = self._read_json_body()
                initial = body.get("initial_dir") or self.manager.default_download_dir
                chosen = _pick_folder_native(initial)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "path": chosen,
                        "disk": _disk_info(chosen) if chosen else None,
                    },
                )
                return

            if path == "/api/disk-info":
                body = self._read_json_body()
                target = body.get("path") or self.manager.default_download_dir
                self._send_json(HTTPStatus.OK, _disk_info(target))
                return

            if path == "/api/downloads":
                body = self._read_json_body()
                url = (body.get("url") or "").strip()
                if not url:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "URL is required"})
                    return
                tasks = self.manager.create_downloads_from_url(
                    url=url,
                    dest_dir=body.get("dest_dir") or None,
                    filename=body.get("filename") or None,
                    expected_sha256=body.get("expected_sha256") or None,
                    max_connections=int(body.get("max_connections") or 16),
                    initial_connections=int(body.get("initial_connections") or 4),
                    adaptive_enabled=bool(body.get("adaptive_enabled", True)),
                    speed_limit_bps=int(body.get("speed_limit_bps") or 0),
                    custom_headers=body.get("custom_headers") or None,
                    mirrors=body.get("mirrors") or None,
                    mode=body.get("mode") or None,
                    source_page_url=body.get("source_page_url") or None,
                    probe_first=bool(body.get("probe_first", True)),
                    audio_url=body.get("audio_url") or None,
                )
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "download": tasks[0].to_dict(),
                        "downloads": [t.to_dict() for t in tasks],
                        "created_count": len(tasks),
                    },
                )
                return

            if path == "/api/downloads/batch":
                body = self._read_json_body()
                items = body.get("items") or []
                if not isinstance(items, list) or not items:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "items array is required"})
                    return
                created_tasks = []
                errors = []
                for item in items:
                    u = (item.get("url") or "").strip()
                    if not u:
                        continue
                    try:
                        t_list = self.manager.create_downloads_from_url(
                            url=u,
                            dest_dir=item.get("dest_dir") or body.get("dest_dir") or None,
                            filename=item.get("filename") or None,
                            max_connections=int(item.get("max_connections") or body.get("max_connections") or 16),
                            adaptive_enabled=bool(item.get("adaptive_enabled", body.get("adaptive_enabled", True))),
                            speed_limit_bps=int(item.get("speed_limit_bps") or body.get("speed_limit_bps") or 0),
                            custom_headers=item.get("custom_headers") or body.get("custom_headers") or None,
                            mode=item.get("mode") or None,
                            source_page_url=item.get("source_page_url") or None,
                            probe_first=bool(item.get("probe_first", True)),
                            audio_url=item.get("audio_url") or None,
                        )
                        created_tasks.extend(t_list)
                    except Exception as exc:
                        errors.append(f"{u}: {exc}")
                if not created_tasks and errors:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "; ".join(errors)})
                    return
                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "downloads": [t.to_dict() for t in created_tasks],
                        "created_count": len(created_tasks),
                        "errors": errors,
                    },
                )
                return

            if path == "/api/downloads/pause-all":
                cnt = self.manager.pause_all()
                self._send_json(HTTPStatus.OK, {"paused": cnt})
                return

            if path == "/api/downloads/resume-all":
                cnt = self.manager.resume_all()
                self._send_json(HTTPStatus.OK, {"resumed": cnt})
                return

            if path == "/api/downloads/retry-failed":
                cnt = self.manager.retry_failed()
                self._send_json(HTTPStatus.OK, {"retried": cnt})
                return

            if path == "/api/downloads/clear-completed":
                cnt = self.manager.clear_completed()
                self._send_json(HTTPStatus.OK, {"cleared": cnt})
                return

            if path == "/api/orphans/clean":
                res = self.manager.clean_orphans()
                self._send_json(HTTPStatus.OK, res)
                return

            if path == "/api/ffmpeg/install":
                ok, msg, ff_path = install_ffmpeg_portable()
                self._send_json(
                    HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST,
                    {
                        "ok": ok,
                        "message": msg,
                        "ffmpeg_path": ff_path,
                        "capabilities": get_capabilities(),
                    },
                )
                return

            if path == "/api/clipboard/dismiss":
                body = self._read_json_body()
                self.manager.dismiss_clipboard_candidate(body.get("url"))
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

            # Action endpoints: /api/downloads/<id>/<action>
            parts = [p for p in path.split("/") if p]
            if len(parts) == 4 and parts[0] == "api" and parts[1] == "downloads":
                dl_id, action = parts[2], parts[3]
                if action == "pause":
                    ok = self.manager.pause(dl_id)
                    self._send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
                    return
                if action == "resume":
                    ok = self.manager.resume(dl_id)
                    self._send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
                    return
                if action == "cancel":
                    ok = self.manager.cancel(dl_id)
                    self._send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
                    return
                if action == "delete":
                    body = self._read_json_body()
                    ok = self.manager.delete(dl_id, remove_file=bool(body.get("remove_file", False)))
                    self._send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok})
                    return
                if action == "verify":
                    ok = self.manager.start_verify_async(dl_id)
                    self._send_json(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST, {"ok": ok})
                    return
                if action == "update-url":
                    task = self.manager.get_task(dl_id)
                    if not task:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                        return
                    body = self._read_json_body()
                    new_url = (body.get("url") or "").strip()
                    add_mirror = bool(body.get("add_as_mirror", False))
                    probe = task.update_url(new_url, add_as_mirror=add_mirror)
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "download": task.to_dict(),
                            "probe_filename": probe.filename,
                        },
                    )
                    return
                if action == "open-folder":
                    task = self.manager.get_task(dl_id)
                    if not task:
                        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False})
                        return
                    ok = _open_in_explorer(task.record.dest_path, task.record.dest_dir)
                    self._send_json(HTTPStatus.OK, {"ok": ok})
                    return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})
        except DownloadError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            try:
                body = self._read_json_body()
                updated = self.manager.update_settings(body)
                self._send_json(HTTPStatus.OK, {"settings": updated})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "downloads":
            dl_id = parts[2]
            task = self.manager.get_task(dl_id)
            if not task:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            try:
                body = self._read_json_body()
                task.update_config(
                    max_connections=body.get("max_connections"),
                    adaptive_enabled=body.get("adaptive_enabled"),
                    speed_limit_bps=body.get("speed_limit_bps"),
                )
                self._send_json(HTTPStatus.OK, {"download": task.to_dict()})
            except Exception as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_sse(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        tick_count = 0
        try:
            while True:
                tasks = self.manager.list_tasks()
                total_speed = sum(float(t["speed_bps"]) for t in tasks)
                payload_obj = {
                    "downloads": tasks,
                    "total_speed_bps": total_speed,
                    "settings": self.manager.get_settings(),
                    "clipboard_candidate": self.manager.get_clipboard_candidate(),
                    "recent_completions": self.manager.get_recent_completions(),
                }
                if tick_count % 6 == 0:
                    payload_obj["orphans"] = self.manager.scan_orphans()
                    payload_obj["disk"] = _disk_info(self.manager.default_download_dir)
                tick_count += 1
                payload = json.dumps(payload_obj)
                chunk = f"data: {payload}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(0.5)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return


def create_server(
    manager: DownloadManager,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> HappydownloadHTTPServer:
    """Create the HTTP server, automatically picking a free port if preferred port is busy."""
    for candidate in (port, 0):
        try:
            return HappydownloadHTTPServer((host, candidate), HappydownloadHandler, manager)
        except OSError:
            continue
    return HappydownloadHTTPServer((host, 0), HappydownloadHandler, manager)

