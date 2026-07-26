#!/usr/bin/env python3
"""Package Lucas Chess R6 as a macOS .app inside a .dmg.

The Mac equivalent of the Windows .exe builds: the result needs no Python, no
compiler and no Homebrew. Users mount the disk image, drag the app to
Applications, and run it.

What it does
------------
1. stages the program tree (bin/ + Resources/) into _build/app_stage, resolving
   the NNUE symlinks and dropping caches and build scratch;
2. primes uci_options.sqlite for every bundled engine, so the installed app
   never needs to write inside its own read-only bundle;
3. converts upstream's logo256r6.ico into an .icns app icon;
4. runs PyInstaller to freeze the interpreter and the third-party libraries,
   carrying the staged tree as data (see app_launcher.py for the layout);
5. ad-hoc code-signs the bundle, which Apple silicon requires;
6. builds a compressed .dmg with the app and an Applications symlink.

Usage
-----
    .venv/bin/python bin/OS/darwin/package_app.py           # full build
    .venv/bin/python bin/OS/darwin/package_app.py --no-dmg  # stop after the .app
"""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent  # bin/OS/darwin
BIN_DIR = HERE.parents[1]  # bin/
ROOT_DIR = BIN_DIR.parent  # repo root
BUILD_DIR = HERE / "_build"
STAGE_DIR = BUILD_DIR / "app_stage"
DIST_DIR = BUILD_DIR / "dist"
WORK_DIR = BUILD_DIR / "pyinstaller"
ICON_ICNS = BUILD_DIR / "LucasChess.icns"
SOURCE_ICO = BIN_DIR / "_genicons" / "lucas" / "logo256r6.ico"

APP_NAME = "Lucas Chess R6"

# Shown in the disk image. macOS 15 removed the old right-click > Open bypass,
# so unsigned apps now have to be approved in System Settings.
GATEKEEPER_INSTRUCTIONS = """The first time you open it, macOS will refuse and say it "could not verify
this app is free of malware". That is because the app is not notarized with a
paid Apple Developer certificate, not because anything is wrong with it.

To open it:
  1. Click Done. Do NOT click "Move to Bin".
  2. Open System Settings > Privacy & Security and scroll down to Security.
  3. Next to the message about Lucas Chess R6, click "Open Anyway", then
     authenticate and confirm.
You only need to do this once.

On macOS 14 and earlier you can instead right-click the app and choose Open.
That shortcut was removed in macOS 15.

If you prefer the Terminal, this does the same thing in one step:
  xattr -dr com.apple.quarantine "/Applications/Lucas Chess R6.app"
"""
BUNDLE_ID = "com.lucaschess.r6.macos"

# Qt ships far more than this program uses (it needs QtCore, QtGui, QtWidgets,
# QtMultimedia, QtSvg and QtSvgWidgets); excluding the rest keeps the download
# to a sane size.
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtTest",
    "PySide6.QtHelp",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtSensors",
    "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech",
    "PySide6.QtNetworkAuth",
    "PySide6.QtSpatialAudio",
    "PySide6.QtHttpServer",
    "matplotlib",
    "tkinter",
    "IPython",
    "pytest",
    "numpy",
]

# The program's own modules are shipped as source and imported at runtime, so
# PyInstaller cannot see which third-party packages they need. Every submodule of
# these gets collected, because the source imports several of them lazily
# (chess.gaviota and chess.syzygy come in via the endgame trainers).
COLLECT_WHOLE_PACKAGES = [
    "chess",
    "deep_translator",
    "bs4",
    "cpuinfo",
    "polib",
    "sortedcontainers",
    "charset_normalizer",
]

HIDDEN_IMPORTS = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "chess",
    "chess.pgn",
    "chess.polyglot",
    "PIL",
    "PIL.Image",
    "PIL.ImageQt",
    "psutil",
    "polib",
    "deep_translator",
    "requests",
    "urllib3",
    "certifi",
    "idna",
    "charset_normalizer",
    "sortedcontainers",
    "bs4",
    "cpuinfo",
    "sqlite3",
]

# Copied from the checkout into the staged tree. Anything not listed is left out
# of the download.
STAGE_ITEMS = [
    ("bin/LucasR.py", "bin/LucasR.py"),
    ("bin/Code", "bin/Code"),
    ("bin/OS/darwin/Engines", "bin/OS/darwin/Engines"),
    ("bin/OS/darwin/OSEngines.py", "bin/OS/darwin/OSEngines.py"),
    # OS/darwin/OSEngines.py reads the engine catalogue out of the Linux one, so
    # that single file has to ship. The Linux engine binaries do not.
    ("bin/OS/linux/OSEngines.py", "bin/OS/linux/OSEngines.py"),
    ("bin/OS/darwin/uci_options.sqlite", "bin/OS/darwin/uci_options.sqlite"),
    ("Resources", "Resources"),
]

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "_build", ".DS_Store", "src.7z", "Src.7z")


def log(msg: str) -> None:
    print(msg, flush=True)


def disk_size_mb(path: Path) -> float:
    """Size on disk, counting hard-linked and cross-linked files once. PyInstaller
    links the bundled data into both Contents/Frameworks and Contents/Resources."""
    seen, total = set(), 0
    for item in path.rglob("*"):
        try:
            info = item.lstat()
        except OSError:
            continue
        if not item.is_file() or item.is_symlink():
            continue
        if info.st_ino in seen:
            continue
        seen.add(info.st_ino)
        total += info.st_size
    return total / 1e6


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, **kwargs)


def discover_imports() -> list[str]:
    """Every module the program's own source imports, found by parsing it.

    The source ships as data and is imported at runtime, so PyInstaller's
    analysis never sees any of it and freezes neither the third-party packages
    nor the parts of the standard library it uses (`wave`, `sqlite3`, `csv`...).
    Reading the imports out of the AST keeps that list correct as upstream
    changes, instead of maintaining it by hand.
    """
    import ast

    modules: set[str] = set()
    sources = [BIN_DIR / "LucasR.py", *(BIN_DIR / "Code").rglob("*.py")]
    for path in sources:
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])

    # Code and OSEngines are the shipped source itself, not frozen modules.
    modules -= {"Code", "OSEngines", "FasterCode"}
    return sorted(modules)


def stage_tree() -> None:
    """Copy the program into a clean staging tree, resolving symlinks.

    OS/darwin/Engines symlinks its big NNUE nets into OS/linux to keep the
    repository small; the app has to carry the real files, and OS/linux is not
    shipped at all."""
    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True)

    for source_rel, dest_rel in STAGE_ITEMS:
        source = ROOT_DIR / source_rel
        dest = STAGE_DIR / dest_rel
        if not source.exists():
            log(f"   warning: {source_rel} missing, skipped")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            # symlinks=False dereferences, so the nets become real files
            shutil.copytree(source, dest, symlinks=False, ignore=IGNORE)
        else:
            shutil.copy2(source, dest)

    # The Windows/Linux engine binaries are not shipped, but their sources are
    # the licence obligation; point users at the repository instead of adding
    # 450 MB to the download.
    (STAGE_DIR / "bin" / "OS" / "darwin" / "SOURCES.txt").write_text(
        "The engines bundled here were compiled from the sources shipped in the\n"
        "Lucas Chess repository, under bin/OS/linux/Engines/<engine>/src.7z:\n"
        "https://github.com/jbh4x82/lucaschessR6/tree/macos-arm64\n"
        "See bin/OS/darwin/build_engines.py for the exact build commands.\n"
    )

    size_mb = disk_size_mb(STAGE_DIR)
    log(f"   staged {size_mb:.0f} MB into {STAGE_DIR.relative_to(ROOT_DIR)}")


def prime_uci_options() -> None:
    """Ask every bundled engine for its UCI options now and cache them in
    OS/darwin/uci_options.sqlite.

    Engines.read_uci_options() writes that cache on a miss. In an installed app
    that path is read-only, so fill it in at build time; it also makes the first
    start noticeably quicker."""
    script = (
        "import os, sys, tempfile\n"
        f"bin_dir = {str(BIN_DIR)!r}\n"
        "os.chdir(bin_dir)\n"
        "sys.argv = [os.path.join(bin_dir, 'LucasR.py')]\n"
        "sys.path.insert(0, bin_dir)\n"
        "from Code.Config import ConfigPaths, Configuration\n"
        "tmp = tempfile.mkdtemp()\n"
        "ConfigPaths.ConfigPaths.LCBASEFOLDER = tmp\n"
        "ConfigPaths.ConfigPaths.LCFILEFOLDER = os.path.join(tmp, 'lc.folder')\n"
        "cfg = Configuration.Configuration('')\n"
        "cfg.lee()\n"
        "done = 0\n"
        "for key, engine in sorted(cfg.engines.dic_engines().items()):\n"
        "    try:\n"
        "        engine.read_uci_options()\n"
        "        done += 1\n"
        "    except Exception as exc:\n"
        "        print('   ' + key + ': ' + str(exc))\n"
        "print('   cached UCI options for %d engines' % done)\n"
    )
    run([sys.executable, "-c", script], cwd=str(BIN_DIR))

    cache = HERE / "uci_options.sqlite"
    if cache.is_file():
        log(f"   {cache.name}: {cache.stat().st_size / 1024:.0f} KB")


def build_icon() -> None:
    """Convert upstream's logo256r6.ico into an .icns."""
    if not SOURCE_ICO.is_file():
        log(f"   warning: {SOURCE_ICO} missing, the app will use the default icon")
        return

    iconset = BUILD_DIR / "LucasChess.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    png = BUILD_DIR / "icon_source.png"
    run(["sips", "-s", "format", "png", str(SOURCE_ICO), "--out", str(png)], capture_output=True)

    for size in (16, 32, 64, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            pixels = size * scale
            out = iconset / f"icon_{size}x{size}{suffix}.png"
            run(
                ["sips", "-z", str(pixels), str(pixels), str(png), "--out", str(out)],
                capture_output=True,
            )

    run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICON_ICNS)])
    log(f"   icon: {ICON_ICNS.name} ({ICON_ICNS.stat().st_size / 1024:.0f} KB)")


def write_spec() -> Path:
    discovered = discover_imports()
    log(f"   {len(discovered)} modules imported by the program's own source")
    spec = BUILD_DIR / "LucasChess.spec"
    spec.write_text(
        f"""# Generated by package_app.py - do not edit, it is overwritten.
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden = list({sorted(set(HIDDEN_IMPORTS) | set(discovered))!r})
for package in {COLLECT_WHOLE_PACKAGES!r}:
    hidden += collect_submodules(package)

a = Analysis(
    [{str(HERE / "app_launcher.py")!r}],
    pathex=[{str(BIN_DIR)!r}],
    binaries=[],
    datas=[({str(STAGE_DIR)!r}, "lucaschess")],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes={EXCLUDES!r},
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LucasChess",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon={str(ICON_ICNS)!r} if {ICON_ICNS.is_file()!r} else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LucasChess",
)

app = BUNDLE(
    coll,
    name={APP_NAME + ".app"!r},
    icon={str(ICON_ICNS)!r} if {ICON_ICNS.is_file()!r} else None,
    bundle_identifier={BUNDLE_ID!r},
    info_plist={{
        "CFBundleName": {APP_NAME!r},
        "CFBundleDisplayName": {APP_NAME!r},
        "CFBundleShortVersionString": "6.0.4",
        "CFBundleVersion": "6.0.4",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "12.0",
        "NSRequiresAquaSystemAppearance": False,
        "CFBundleDocumentTypes": [
            {{
                "CFBundleTypeName": "Chess game",
                "CFBundleTypeExtensions": ["pgn", "lcdb", "lcsb", "bmt"],
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Alternate",
            }}
        ],
    }},
)
"""
    )
    return spec


def build_app(spec: Path) -> Path:
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath",
            str(DIST_DIR),
            "--workpath",
            str(WORK_DIR),
            str(spec),
        ],
        cwd=str(BUILD_DIR),
    )
    app = DIST_DIR / f"{APP_NAME}.app"
    if not app.is_dir():
        raise SystemExit(f"PyInstaller did not produce {app}")
    return app


def fix_and_sign(app: Path) -> None:
    """Make every bundled engine executable again and ad-hoc sign the bundle.

    PyInstaller copies data files without their exec bit, and code on Apple
    silicon has to carry at least an ad-hoc signature to run at all."""
    engines_dir = app / "Contents" / "Resources" / "lucaschess" / "bin" / "OS" / "darwin" / "Engines"
    fixed = 0
    for path in engines_dir.rglob("*"):
        if path.is_file() and path.read_bytes()[:4] in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"):
            path.chmod(0o755)
            fixed += 1
    log(f"   restored the executable bit on {fixed} engine binaries")

    # Sign the engines first, then the bundle as a whole.
    for path in sorted(engines_dir.rglob("*")):
        if path.is_file() and os.access(path, os.X_OK):
            subprocess.run(
                ["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
                capture_output=True,
            )
    run(["codesign", "--force", "--deep", "--sign", "-", "--timestamp=none", str(app)], capture_output=True)
    result = subprocess.run(["codesign", "--verify", "--verbose=2", str(app)], capture_output=True, text=True)
    log(f"   codesign --verify: {'ok' if result.returncode == 0 else result.stderr.strip()}")


def build_dmg(app: Path) -> Path:
    dmg_dir = BUILD_DIR / "dmg"
    if dmg_dir.exists():
        shutil.rmtree(dmg_dir)
    dmg_dir.mkdir(parents=True)

    shutil.copytree(app, dmg_dir / app.name, symlinks=True)
    (dmg_dir / "Applications").symlink_to("/Applications")
    (dmg_dir / "READ ME.txt").write_text(
        f"{APP_NAME} for macOS (Apple Silicon)\n"
        "=======================================\n\n"
        "Drag the app onto the Applications folder, then open it from there.\n\n"
        + GATEKEEPER_INSTRUCTIONS
        + "\n"
        "Settings, games and results are kept in\n"
        "  ~/Library/Application Support/Lucas Chess R6\n"
        "so deleting the app leaves your data alone.\n\n"
        "Source, build scripts and the list of bundled engines:\n"
        "  https://github.com/jbh4x82/lucaschessR6/tree/macos-arm64\n\n"
        "Lucas Chess is by Lucas Monge and is licensed under the GPL v3.\n"
    )

    dmg = DIST_DIR / f"LucasChessR6-macOS-arm64.dmg"
    dmg.unlink(missing_ok=True)
    run(
        [
            "hdiutil",
            "create",
            "-volname",
            APP_NAME,
            "-srcfolder",
            str(dmg_dir),
            "-ov",
            "-format",
            "UDZO",
            "-imagekey",
            "zlib-level=9",
            str(dmg),
        ],
        capture_output=True,
    )
    return dmg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-dmg", action="store_true", help="stop after building the .app")
    args = parser.parse_args()

    if sys.platform != "darwin":
        log("error: this only runs on macOS")
        return 1

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()

    log(":: 1/6 priming the UCI option cache")
    prime_uci_options()

    log(":: 2/6 staging the program tree")
    stage_tree()

    log(":: 3/6 building the app icon")
    build_icon()

    log(":: 4/6 running PyInstaller (this takes a few minutes)")
    app = build_app(write_spec())
    app_mb = disk_size_mb(app)
    log(f"   {app.name}: {app_mb:.0f} MB")

    log(":: 5/6 signing")
    fix_and_sign(app)

    if args.no_dmg:
        log(f"\n:: done in {time.time() - started:.0f}s -> {app}")
        return 0

    log(":: 6/6 building the disk image")
    dmg = build_dmg(app)
    log(f"   {dmg.name}: {dmg.stat().st_size / 1e6:.0f} MB")

    log(f"\n:: done in {time.time() - started:.0f}s")
    log(f"   app: {app}")
    log(f"   dmg: {dmg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
