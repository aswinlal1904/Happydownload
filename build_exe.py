"""Build script to compile Happydownload v2.0.0 into standalone Windows .exe executables.

Produces:
1. `Happydownload.exe` (in project root): Fast native Win32 GUI launcher with custom icon & version metadata.
2. `dist/Happydownload.exe` (via PyInstaller): Self-contained single-file portable Windows executable
   bundling Python, yt-dlp, and the Happydownload UI/engine for distribution to PCs without Python.
   Pass `--bundle-ffmpeg` to also embed `ffmpeg.exe` directly inside the PyInstaller single-file `.exe`
   (by default, `Happydownload.exe` stays lightweight for GitHub's <100 MB limit and automatically
   discovers or auto-installs portable `ffmpeg.exe` into `data/bin/ffmpeg.exe` on other users' PCs).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from happydownload import __author__, __version__
from happydownload.extractor import find_ffmpeg

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_PATH = os.path.join(ROOT_DIR, "happydownload", "static", "favicon.ico")
STATIC_DIR = os.path.join(ROOT_DIR, "happydownload", "static")


def build_native_root_launcher() -> str:
    """Compile a zero-console native Win32 GUI launcher `Happydownload.exe` in the project root."""
    csc_candidates = [
        shutil.which("csc"),
        r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    ]
    csc_bin = next((c for c in csc_candidates if c and os.path.isfile(c)), None)
    out_exe = os.path.join(ROOT_DIR, "Happydownload.exe")
    if not csc_bin:
        print("csc.exe not found; skipping root launcher build.")
        return out_exe

    cs_source = rf'''using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("Happydownload")]
[assembly: AssemblyDescription("Adaptive Multi-Stream 50+ GB & 4K Video Download Engine")]
[assembly: AssemblyCompany("{__author__}")]
[assembly: AssemblyProduct("Happydownload v{__version__}")]
[assembly: AssemblyCopyright("Copyright (c) 2026 {__author__}")]
[assembly: AssemblyVersion("{__version__}.0")]
[assembly: AssemblyFileVersion("{__version__}.0")]

namespace HappydownloadLauncher {{
    static class Program {{
        [STAThread]
        static int Main(string[] args) {{
            string baseDir = AppDomain.CurrentDomain.BaseDirectory;
            string distExe = Path.Combine(baseDir, "dist", "Happydownload.exe");
            string mainPy = Path.Combine(baseDir, "main.py");
            string joinedArgs = args.Length > 0 ? string.Join(" ", args) : "--desktop";

            try {{
                if (File.Exists(mainPy)) {{
                    ProcessStartInfo psi = new ProcessStartInfo();
                    psi.FileName = "pythonw.exe";
                    psi.Arguments = "\"" + mainPy + "\" " + joinedArgs;
                    psi.WorkingDirectory = baseDir;
                    psi.UseShellExecute = false;
                    psi.CreateNoWindow = true;
                    try {{
                        Process.Start(psi);
                        return 0;
                    }} catch {{
                        psi.FileName = "python.exe";
                        Process.Start(psi);
                        return 0;
                    }}
                }} else if (File.Exists(distExe)) {{
                    ProcessStartInfo psi = new ProcessStartInfo(distExe, joinedArgs);
                    psi.WorkingDirectory = baseDir;
                    psi.UseShellExecute = false;
                    Process.Start(psi);
                    return 0;
                }} else {{
                    MessageBox.Show(
                        "Could not locate main.py or dist\\Happydownload.exe in " + baseDir,
                        "Happydownload v{__version__}",
                        MessageBoxButtons.OK,
                        MessageBoxIcon.Error
                    );
                    return 1;
                }}
            }} catch (Exception ex) {{
                MessageBox.Show(
                    "Failed to launch Happydownload: " + ex.Message,
                    "Happydownload v{__version__}",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }}
        }}
    }}
}}
'''
    cs_file = os.path.join(ROOT_DIR, "_launcher_build.cs")
    try:
        with open(cs_file, "w", encoding="utf-8") as f:
            f.write(cs_source)
        cmd = [
            csc_bin,
            "/nologo",
            "/target:winexe",
            "/optimize+",
            f"/out:{out_exe}",
            f"/win32icon:{ICON_PATH}",
            "/reference:System.Windows.Forms.dll",
            cs_file,
        ]
        subprocess.run(cmd, check=True)
        print(f"[OK] Built root Win32 launcher: {out_exe}")
    finally:
        if os.path.exists(cs_file):
            os.remove(cs_file)
    return out_exe


def build_pyinstaller_standalone(bundle_ffmpeg: bool = False) -> str:
    """Build self-contained standalone `dist/Happydownload.exe` using PyInstaller."""
    version_file = os.path.join(ROOT_DIR, "_pyi_version_info.txt")
    v_parts = (__version__.split(".") + ["0", "0", "0"])[:4]
    v_tuple = ", ".join(str(int(x)) for x in v_parts)
    version_info_txt = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v_tuple}),
    prodvers=({v_tuple}),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{__author__}'),
          StringStruct('FileDescription', 'Happydownload v{__version__} — Adaptive 50+ GB & 4K Video Downloader'),
          StringStruct('FileVersion', '{__version__}.0'),
          StringStruct('InternalName', 'Happydownload'),
          StringStruct('LegalCopyright', 'Copyright (c) 2026 {__author__}'),
          StringStruct('OriginalFilename', 'Happydownload.exe'),
          StringStruct('ProductName', 'Happydownload'),
          StringStruct('ProductVersion', '{__version__}.0')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    with open(version_file, "w", encoding="utf-8") as vf:
        vf.write(version_info_txt)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--noconsole",
        "--name", "Happydownload",
        "--icon", ICON_PATH,
        "--version-file", version_file,
        "--add-data", f"{STATIC_DIR};happydownload/static",
        "--collect-all", "yt_dlp",
    ]

    if bundle_ffmpeg:
        ff = find_ffmpeg()
        if ff and os.path.isfile(ff):
            print(f"Bundling ffmpeg.exe from: {ff}")
            cmd.extend(["--add-binary", f"{ff};bin"])

    cmd.append(os.path.join(ROOT_DIR, "main.py"))
    try:
        subprocess.run(cmd, cwd=ROOT_DIR, check=True)
    finally:
        if os.path.exists(version_file):
            os.remove(version_file)
        spec_file = os.path.join(ROOT_DIR, "Happydownload.spec")
        if os.path.exists(spec_file):
            os.remove(spec_file)

    dist_exe = os.path.join(ROOT_DIR, "dist", "Happydownload.exe")
    print(f"[OK] Built standalone PyInstaller executable: {dist_exe}")
    return dist_exe


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Happydownload .exe executables.")
    parser.add_argument(
        "--bundle-ffmpeg",
        action="store_true",
        help="Bundle ffmpeg.exe inside dist/Happydownload.exe (increases exe size above 100 MB).",
    )
    parser.add_argument(
        "--launcher-only",
        action="store_true",
        help="Only build the lightweight root Happydownload.exe launcher.",
    )
    args = parser.parse_args()

    build_native_root_launcher()
    if not args.launcher_only:
        build_pyinstaller_standalone(bundle_ffmpeg=args.bundle_ffmpeg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
