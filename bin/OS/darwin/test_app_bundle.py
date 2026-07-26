#!/usr/bin/env python3
"""Test the packaged .app the way a user actually receives it.

Nothing here uses the source tree or the project venv: it takes the .dmg,
marks it as a browser download, mounts it, installs the app the way dragging it
onto Applications would, and then checks that the installed copy runs.

    python3 bin/OS/darwin/test_app_bundle.py                  # full run
    python3 bin/OS/darwin/test_app_bundle.py --keep           # leave it installed
    python3 bin/OS/darwin/test_app_bundle.py --dmg path.dmg   # a specific image

Steps
-----
1. quarantine the disk image, as Safari would
2. mount it and check it looks like an installer (app + Applications symlink)
3. copy the app to the install directory, and confirm the copy is quarantined too
4. report the Gatekeeper verdict, then clear quarantine, which is what
   approving the app under System Settings > Privacy & Security does
5. make the frozen bundle import every module it ships, which is the only way
   to catch a third-party package PyInstaller did not freeze
6. launch it, and check it stays up, writes no traceback, and puts its user data
   under ~/Library/Application Support rather than inside the bundle
7. drive the engines that ship inside the installed bundle over UCI
8. relaunch, to confirm the settings it just wrote are readable
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DMG = HERE / "_build" / "dist" / "LucasChessR6-macOS-arm64.dmg"
APP_NAME = "Lucas Chess R6.app"
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Lucas Chess R6"

sys.path.insert(0, str(HERE))
from uci_probe import MATE_IN_1_FEN, MATE_IN_1_MOVE, UCIEngine  # noqa: E402

# Engines to drive inside the installed bundle. Stockfish exercises the NNUE
# files, Irina is the one upstream could never build for macOS.
ENGINES_TO_PROBE = [
    ("stockfish", "stockfish/stockfish-18-64"),
    ("irina", "irina/irina"),
    ("ethereal", "ethereal/Ethereal-12.75"),
]

PASS, FAIL = [], []


def check(what: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASS.append(what)
        print(f"  \033[32mpass\033[0m {what}" + (f"  {detail}" if detail else ""), flush=True)
        return True
    FAIL.append((what, detail))
    print(f"  \033[31mFAIL\033[0m {what}" + (f"  {detail}" if detail else ""), flush=True)
    return False


def step(msg: str) -> None:
    print(f"\n\033[1m{msg}\033[0m", flush=True)


def sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def has_quarantine(path: Path) -> bool:
    return "com.apple.quarantine" in sh(["xattr", str(path)]).stdout


def mount(dmg: Path) -> Path:
    result = sh(["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(dmg)])
    if result.returncode != 0:
        raise SystemExit(f"could not mount {dmg}: {result.stderr}")
    plist = plistlib.loads(result.stdout.encode())
    for entity in plist["system-entities"]:
        if "mount-point" in entity:
            return Path(entity["mount-point"])
    raise SystemExit("the image mounted with no mount point")


def unmount(mount_point: Path) -> None:
    sh(["hdiutil", "detach", str(mount_point), "-force"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dmg", type=Path, default=DEFAULT_DMG)
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=Path("/Applications"),
        help="where to install (default /Applications, as a user would)",
    )
    parser.add_argument("--keep", action="store_true", help="do not uninstall afterwards")
    parser.add_argument("--seconds", type=int, default=25, help="how long to let the app run")
    args = parser.parse_args()

    if not args.dmg.is_file():
        raise SystemExit(f"no disk image at {args.dmg} (run package_app.py first)")

    installed = args.install_dir / APP_NAME
    print(f"disk image: {args.dmg}  ({args.dmg.stat().st_size / 1e6:.0f} MB)")
    print(f"installing to: {installed}")

    # The app writes its settings here; start from a clean slate so we can prove
    # a fresh install works, but keep anything that was already there.
    stashed = None
    if SUPPORT_DIR.exists():
        stashed = SUPPORT_DIR.with_name(SUPPORT_DIR.name + ".test-stash")
        shutil.rmtree(stashed, ignore_errors=True)
        SUPPORT_DIR.rename(stashed)

    mount_point = None
    try:
        step("1. marking the download as quarantined, the way a browser does")
        sh(["xattr", "-w", "com.apple.quarantine", "0083;00000000;Safari;", str(args.dmg)])
        check("disk image carries com.apple.quarantine", has_quarantine(args.dmg))

        step("2. mounting the disk image")
        mount_point = mount(args.dmg)
        contents = sorted(p.name for p in mount_point.iterdir() if not p.name.startswith("."))
        check("image mounted", mount_point.is_dir(), str(mount_point))
        check("contains the app", APP_NAME in contents, ", ".join(contents))
        check("contains an Applications shortcut to drag onto", "Applications" in contents)

        step("3. installing it, as dragging onto Applications would")
        if installed.exists():
            shutil.rmtree(installed)
        sh(["cp", "-R", str(mount_point / APP_NAME), str(installed)])
        check("app copied to the install directory", installed.is_dir())
        check(
            "the installed copy inherited the quarantine flag",
            has_quarantine(installed),
            "this is what makes macOS warn on first open",
        )

        step("4. Gatekeeper verdict (unsigned build, so a warning is expected)")
        verdict = sh(["spctl", "-a", "-vvv", "--type", "execute", str(installed)])
        gatekeeper = (verdict.stderr or verdict.stdout).strip().replace("\n", " ")
        print(f"       spctl: {gatekeeper}")
        signature = sh(["codesign", "--verify", "--deep", "--verbose=2", str(installed)])
        check(
            "the bundle's ad-hoc signature is intact",
            signature.returncode == 0,
            signature.stderr.strip().split("\n")[-1] if signature.returncode else "",
        )
        # macOS 15 removed the Control-click > Open bypass, so the user's route is
        # System Settings > Privacy & Security > Open Anyway. Clearing the flag is
        # the scriptable equivalent of that approval.
        sh(["xattr", "-dr", "com.apple.quarantine", str(installed)])
        check("quarantine cleared, as approving it in System Settings does", not has_quarantine(installed))

        step("5. checking the frozen bundle can import every module it ships")
        executable = installed / "Contents" / "MacOS" / "LucasChess"
        selftest = subprocess.run(
            [str(executable)],
            capture_output=True,
            text=True,
            env={**os.environ, "LUCASCHESS_SELFTEST": "imports"},
            cwd=str(Path.home()),
            timeout=600,
        )
        report = (selftest.stdout + selftest.stderr).strip()
        summary = next((line for line in report.splitlines() if line.startswith("checked ")), "")
        missing = [line for line in report.splitlines() if line.startswith("IMPORT FAIL")]
        check(
            "no third-party module is missing from the bundle",
            not missing and selftest.returncode == 0,
            summary if not missing else "; ".join(missing[:3]),
        )

        step("6. launching the installed app")
        check("bundle executable is present and executable", os.access(executable, os.X_OK), str(executable.name))

        proc = subprocess.Popen(
            [str(executable)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(Path.home()),
        )
        time.sleep(args.seconds)
        alive = proc.poll() is None
        output = ""
        if not alive:
            output = (proc.stdout.read() if proc.stdout else "").strip()
            check(f"still running after {args.seconds}s", False, output[-600:])
        else:
            check(f"still running after {args.seconds}s", True, f"pid {proc.pid}")

        # A crash before the log is redirected only shows on the process output,
        # so check both rather than trusting an empty bug.log.
        bug_log = SUPPORT_DIR / "bug.log"
        log_text = bug_log.read_text(errors="ignore") if bug_log.is_file() else ""
        combined = log_text + output
        check("no traceback while starting up", "Traceback" not in combined, combined.strip()[-500:])

        check(
            "user data went to ~/Library/Application Support",
            (SUPPORT_DIR / "UserData").is_dir(),
            str(SUPPORT_DIR / "UserData"),
        )
        in_bundle = installed / "Contents" / "Resources" / "lucaschess" / "UserData"
        check("nothing was written inside the app bundle", not in_bundle.exists())

        config = SUPPORT_DIR / "UserData" / "__Config__"
        check("settings were created", config.is_dir(), ", ".join(sorted(p.name for p in config.glob("*"))[:4]))

        if alive:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()

        step("7. driving the engines that ship inside the installed app")
        engines_dir = installed / "Contents" / "Resources" / "lucaschess" / "bin" / "OS" / "darwin" / "Engines"
        found = sorted(p.name for p in engines_dir.iterdir() if p.is_dir()) if engines_dir.is_dir() else []
        check("engines are inside the bundle", len(found) >= 18, f"{len(found)} engines")

        nets = engines_dir / "stockfish"
        real_nets = [p for p in nets.glob("*.nnue") if p.is_file() and not p.is_symlink()]
        check(
            "Stockfish's NNUE nets are real files in the bundle, not symlinks",
            len(real_nets) == 2,
            f"{sum(p.stat().st_size for p in real_nets) / 1e6:.0f} MB",
        )

        for key, relative in ENGINES_TO_PROBE:
            path_exe = engines_dir / relative
            if not check(f"[{key}] binary present", path_exe.is_file(), str(relative)):
                continue
            try:
                with UCIEngine(path_exe) as engine:
                    name = engine.handshake()
                    check(f"[{key}] UCI handshake from inside the bundle", bool(name), name)
                    mate = engine.bestmove(MATE_IN_1_FEN, depth=6)
                    check(f"[{key}] finds the mate in one", mate == MATE_IN_1_MOVE, mate)
            except Exception as exc:
                check(f"[{key}] runs from inside the bundle", False, f"{type(exc).__name__}: {exc}")

        step("8. second launch, to confirm it reads back the settings it wrote")
        proc2 = subprocess.Popen(
            [str(executable)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(Path.home())
        )
        time.sleep(15)
        alive2 = proc2.poll() is None
        output2 = "" if alive2 else (proc2.stdout.read() if proc2.stdout else "")
        check("second launch stays up", alive2, output2.strip()[-400:])
        log_text2 = bug_log.read_text(errors="ignore") if bug_log.is_file() else ""
        check("still no traceback", "Traceback" not in log_text2, log_text2.strip()[-400:])
        if alive2:
            proc2.terminate()
            try:
                proc2.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc2.kill()

    finally:
        if mount_point is not None:
            unmount(mount_point)
        if not args.keep and installed.exists():
            shutil.rmtree(installed, ignore_errors=True)
            print(f"\nuninstalled {installed}")
        if stashed is not None:
            shutil.rmtree(SUPPORT_DIR, ignore_errors=True)
            stashed.rename(SUPPORT_DIR)
            print(f"restored your existing {SUPPORT_DIR.name}")

    print()
    print("=" * 62)
    print(f"passed {len(PASS)}   failed {len(FAIL)}")
    for what, detail in FAIL:
        print(f"  - {what}" + (f"  ({detail})" if detail else ""))
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
