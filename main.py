"""Happydownload entry point: launches the adaptive downloader Web UI or CLI."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser

from happydownload import __author__, __version__
from happydownload.engine import DownloadManager
from happydownload.models import DownloadStatus
from happydownload.server import create_server
from happydownload.tray import start_tray_icon


def _default_dirs() -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        base_dir = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, "data")
    downloads_dir = os.path.join(base_dir, "downloads")
    return data_dir, downloads_dir


def open_desktop_app_window(url: str, data_dir: str | None = None) -> bool:
    """Launch a standalone chromeless native desktop window via Edge/Chrome --app mode."""
    profile_dir = os.path.join(data_dir or _default_dirs()[0], "desktop_window_profile")
    os.makedirs(profile_dir, exist_ok=True)
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for bin_path in candidates:
        if bin_path and os.path.isfile(bin_path):
            try:
                subprocess.Popen(
                    [
                        bin_path,
                        f"--app={url}",
                        f"--user-data-dir={profile_dir}",
                        "--window-size=1420,900",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            except Exception:
                continue
    webbrowser.open(url)
    return False


def run_ui(
    host: str,
    port: int,
    open_browser: bool,
    dest_dir: str,
    data_dir: str,
    use_tray: bool = False,
    desktop_mode: bool = True,
) -> int:
    manager = DownloadManager(data_dir=data_dir, default_download_dir=dest_dir)
    server = create_server(manager, host=host, port=port)
    actual_host, actual_port = server.server_address[:2]
    url = f"http://{actual_host}:{actual_port}/"

    mode_label = "Standalone Desktop Window" if desktop_mode else "Web Browser UI"
    print(f"Happydownload v{__version__} (Developed by {__author__})")
    print(f"Mode:                    {mode_label}")
    print(f"Engine URL:              {url}")
    print(f"Default download folder: {manager.default_download_dir}")
    print("Press Ctrl+C (or use the Windows System Tray icon) to pause active downloads safely and exit.")

    def _open_ui() -> None:
        if desktop_mode:
            open_desktop_app_window(url, data_dir=data_dir)
        else:
            webbrowser.open(url)

    if open_browser:
        _open_ui()

    def _trigger_shutdown() -> None:
        server.shutdown()

    if use_tray or desktop_mode:
        start_tray_icon(
            ui_url=url,
            on_pause_all=manager.pause_all,
            on_resume_all=manager.resume_all,
            on_exit=_trigger_shutdown,
            on_open_ui=_open_ui,
        )

    def _shutdown_handler(_signum, _frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, _shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown_handler)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down cleanly — checkpointing active segments...")
    finally:
        server.shutdown()
        server.server_close()
        manager.shutdown()
    return 0


def run_cli_download(args: argparse.Namespace) -> int:
    data_dir, default_dl_dir = _default_dirs()
    dest_dir = args.dest or default_dl_dir
    manager = DownloadManager(data_dir=args.data_dir or data_dir, default_download_dir=dest_dir)
    try:
        task = manager.create_download(
            url=args.url,
            dest_dir=dest_dir,
            filename=args.filename,
            expected_sha256=args.sha256,
            max_connections=args.max_connections,
            adaptive_enabled=not args.no_adaptive,
            speed_limit_bps=max(0, args.limit_kb) * 1024,
        )
        print(f"Downloading: {task.record.filename}")
        while task.is_running():
            snap = task.to_dict()
            total = snap["total_bytes"]
            done = snap["downloaded_bytes"]
            speed_mb = float(snap["speed_bps"]) / (1024 * 1024)
            pct = snap["progress"]
            conns = snap["active_connections"]
            target = snap["target_connections"]
            total_str = f"{total / (1024 * 1024):.1f} MB" if total else "?"
            print(
                f"\r[{snap['status']}] {done / (1024 * 1024):.1f}/{total_str} ({pct}%) | "
                f"{speed_mb:.2f} MB/s | conns {conns}/{target}   ",
                end="",
                flush=True,
            )
            time.sleep(0.5)
        print()
        final = task.to_dict()
        print(f"Final status: {final['status']} -> {final['dest_path']}")
        return 0 if task.record.status == DownloadStatus.COMPLETED else 1
    except KeyboardInterrupt:
        print("\nPaused safely. Re-run or open UI to resume with zero data loss.")
        return 130
    finally:
        manager.shutdown()


def main(argv=None) -> int:
    data_dir, downloads_dir = _default_dirs()
    parser = argparse.ArgumentParser(
        prog="happydownload",
        description=f"Happydownload v{__version__} by {__author__} — Adaptive multi-stream downloader for 50+ GB files & web videos.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Happydownload v{__version__} (Developed by {__author__})",
    )
    sub = parser.add_subparsers(dest="command")

    ui_p = sub.add_parser("ui", help="Start the Happydownload Standalone Desktop App or Web UI (default)")
    ui_p.add_argument("--host", default="127.0.0.1")
    ui_p.add_argument("--port", type=int, default=8765)
    ui_p.add_argument("--no-browser", action="store_true", help="Do not auto-open window on startup")
    ui_p.add_argument("--tray", action="store_true", help="Show Windows system tray icon")
    ui_p.add_argument("--desktop", action="store_true", default=True, help="Launch as a standalone desktop window (default)")
    ui_p.add_argument("--web", action="store_true", help="Open in standard web browser tab instead of standalone desktop window")
    ui_p.add_argument("--dest", default=downloads_dir, help="Default download directory")
    ui_p.add_argument("--data-dir", default=data_dir, help="Database/state directory")

    dl_p = sub.add_parser("download", help="Download a URL directly in CLI mode")
    dl_p.add_argument("url", help="Direct HTTP/HTTPS file or video page URL")
    dl_p.add_argument("--dest", default=downloads_dir, help="Destination directory")
    dl_p.add_argument("--filename", default=None, help="Override output filename")
    dl_p.add_argument("--sha256", default=None, help="Expected SHA-256 hex digest")
    dl_p.add_argument("--max-connections", type=int, default=16)
    dl_p.add_argument("--no-adaptive", action="store_true")
    dl_p.add_argument("--limit-kb", type=int, default=0, help="Speed limit in KB/s (0 = unlimited)")
    dl_p.add_argument("--data-dir", default=data_dir)

    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--tray", action="store_true", help="Show Windows system tray icon")
    parser.add_argument("--desktop", action="store_true", default=True, help="Launch as a standalone desktop window (default)")
    parser.add_argument("--web", action="store_true", help="Open in standard web browser tab instead of standalone desktop window")
    parser.add_argument("--dest", default=downloads_dir)
    parser.add_argument("--data-dir", default=data_dir)

    args = parser.parse_args(argv)
    if args.command == "download":
        return run_cli_download(args)
    use_desktop = bool(args.desktop and not args.web)
    return run_ui(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        dest_dir=args.dest,
        data_dir=args.data_dir,
        use_tray=args.tray or use_desktop,
        desktop_mode=use_desktop,
    )


if __name__ == "__main__":
    sys.exit(main())

