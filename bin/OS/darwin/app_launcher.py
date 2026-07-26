"""Entry point of the packaged macOS app (see package_app.py).

The .app carries the whole program tree - bin/ and Resources/ - as data, laid
out exactly as in a source checkout:

    Lucas Chess R6.app/Contents/Resources/lucaschess/bin/{LucasR.py,Code,OS,...}
    Lucas Chess R6.app/Contents/Resources/lucaschess/Resources/...

so this launcher only has to make the process look like a normal `python
LucasR.py` start-up and then run LucasR.py itself with runpy. Nothing about the
argument handling is duplicated here, so opening a .pgn, -tournament, -kibitzer
and the rest keep working exactly as upstream defines them.

Two things do have to be redirected, because the application bundle is
read-only once it is installed in /Applications:

* user data (settings, games, results) goes to ~/Library/Application Support
* bug.log goes next to it, instead of into bin/
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = BUNDLE_DIR / "lucaschess"
BIN_DIR = APP_DIR / "bin"

SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Lucas Chess R6"


def _redirect_logs_to_support_dir(support_dir: Path) -> None:
    """Code.Main.Init points sys.stderr at a relative "bug.log", which would land
    inside the read-only bundle. Wrap the two log classes so any relative log
    name is written to the support folder instead."""
    from Code.Z import Debug, Util

    def relocate(cls):
        class Relocated(cls):
            def __init__(self, logname, *args, **kwargs):
                path = Path(logname)
                if not path.is_absolute():
                    path = support_dir / path.name
                super().__init__(path, *args, **kwargs)

        Relocated.__name__ = cls.__name__
        return Relocated

    Util.Log = relocate(Util.Log)
    Debug.LogDebug = relocate(Debug.LogDebug)


def _selftest_imports() -> int:
    """Import every module of the program inside the packaged app.

    The app ships its own source and imports it at runtime, so PyInstaller
    cannot see which third-party packages that source needs; a missing one only
    shows up when the module is first imported, which may be deep in a menu.
    Walking the tree here surfaces all of them at once.

    Only ImportError counts as a failure. Modules that need the translation
    globals or a live configuration at import time raise other errors, and those
    say nothing about whether the package is complete.
    """
    import importlib

    failures = []
    checked = 0
    # PyInstaller puts the data in Contents/Resources and symlinks it into
    # Contents/Frameworks, one link per top-level entry. bin/Code is one of those
    # links, and rglob does not descend into a symlinked directory, so resolve it.
    code_dir = (BIN_DIR / "Code").resolve()
    for path in sorted(code_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(code_dir)
        module = "Code." + ".".join(relative.with_suffix("").parts)
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        checked += 1
        try:
            importlib.import_module(module)
        except ImportError as exc:
            failures.append(f"IMPORT FAIL {module}: {exc}")
        except Exception as exc:  # not a packaging problem
            print(f"skip {module}: {type(exc).__name__}: {exc}")

    print(f"checked {checked} modules, {len(failures)} import failures")
    for line in failures:
        print(line)
    return 1 if failures else 0


def main() -> None:
    args = sys.argv[1:]

    # Code.Z.XRun restarts the program with [sys.executable, "LucasR.py", *args]
    # when sys.argv[0] ends in .py, which it does below. In a packaged app
    # sys.executable is this launcher, so drop the script name it prepends.
    if args and Path(args[0]).name == "LucasR.py":
        args = args[1:]

    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

    # Code/__init__.py derives every path from sys.argv[0] and chdir()s there.
    os.chdir(BIN_DIR)
    sys.argv = [str(BIN_DIR / "LucasR.py")] + args
    sys.path.insert(0, str(BIN_DIR))

    from Code.Config import ConfigPaths

    ConfigPaths.ConfigPaths.LCBASEFOLDER = str(SUPPORT_DIR / "UserData")
    ConfigPaths.ConfigPaths.LCFILEFOLDER = str(SUPPORT_DIR / "lc.folder")

    _redirect_logs_to_support_dir(SUPPORT_DIR)

    # Used by test_app_bundle.py to verify the frozen bundle is complete.
    if os.environ.get("LUCASCHESS_SELFTEST") == "imports":
        sys.exit(_selftest_imports())

    runpy.run_path(str(BIN_DIR / "LucasR.py"), run_name="__main__")


if __name__ == "__main__":
    main()
