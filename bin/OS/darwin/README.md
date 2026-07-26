macOS support for Lucas Chess R6
================================

Upstream ships binaries for Windows and Linux only. This folder is the macOS
equivalent of `bin/OS/linux`: `Code/__init__.py` resolves `bin/OS/<platform>`
and puts it on `sys.path`, so on macOS it finds `OSEngines.py` and the engines
here.

<p align="center">
  <img src="../../../docs/screenshot-macos.png" alt="Lucas Chess R 6.0.4 running natively on macOS (Apple Silicon)" width="560">
</p>

    build_engines.py   builds the bundled engine sources for macOS
    OSEngines.py       the engine catalogue (reuses the Linux one, see below)
    run_tests.py       verification suite for the whole macOS build
    Engines/           the macOS engine binaries and their data files
    _build/            scratch build tree and per-engine build logs (disposable)


Setting it up from a fresh clone
--------------------------------

    brew install git-lfs sevenzip           # LFS is mandatory, see below
    git lfs install
    git clone https://github.com/lukasmonk/lucaschessR6.git
    cd lucaschessR6

    python3.12 -m venv .venv
    .venv/bin/pip install -r requirements.txt cython

    bin/_fastercode/fastercode_macos.sh     # build the FasterCode extension
    python3 bin/OS/darwin/build_engines.py  # build the engines
    .venv/bin/python bin/OS/darwin/run_tests.py

Then start it with `./LucasChess.command`, or `cd bin && ../.venv/bin/python LucasR.py`.

**Git LFS is not optional.** The engines, opening books and tablebases are LFS
objects. Cloning without `git-lfs` installed leaves pointer files behind and
the checkout aborts halfway through; `run_tests.py resources` checks for this.


What is macOS-specific
----------------------

* `bin/_fastercode/fastercode_macos.sh` + `src/setup_macos.py` - build the
  Cython/C extension with clang instead of gcc, and drop the `-march=x86-64`
  flags. `src/irina/util.c` needed `<sys/select.h>`/`<sys/time.h>`, which glibc
  pulls in via `<sys/types.h>` but macOS libc does not.
* `Code/__init__.py` - `platform` resolves to `darwin`, and `font_mono` is
  Menlo (macOS has no "Mono" family).
* `Code/Z/Util.py` - added `is_macos()` and `is_posix()`. Call sites that meant
  "POSIX" rather than "Linux" (file permissions, nice values, file dialogs) now
  use `is_posix()`; `is_linux()` still means Linux only, for the e-board
  drivers and the Wayland check.
* `Code/Engines/EngineRun.py` - sets `DYLD_LIBRARY_PATH` instead of
  `LD_LIBRARY_PATH` for engines that load a library next to the binary.
* `Code/Engines/CheckEngines.py` - the Stockfish CPU-variant selection only
  applies to the x86 builds, which ship as a family of binaries. On Apple
  silicon there is one build compiled for the host, so the check exits early.


How OSEngines.py works here
---------------------------

The catalogue (names, authors, elo, UCI defaults, the Maia setup) is
platform-independent, so `OSEngines.py` loads `OS/linux/OSEngines.py` and only
changes what differs on macOS: engines are looked up in `OS/darwin/Engines`, and
**only the engines whose binary is actually present get registered**. Not every
bundled engine compiles for arm64, and the upstream catalogue assumes they are
all there.

That means building one more engine is enough to make it appear in the app - no
edit to `OSEngines.py` is needed.


Adding or fixing an engine
--------------------------

Add a `Recipe` to `build_engines.py`. The `exe` field must match the file name
used in `OS/linux/OSEngines.py`, since that is the catalogue being reused.

Builds run with compiler shims (`_build/shims`) first on `PATH`. These drop the
x86-only and Linux-only switches these makefiles pass unconditionally
(`-msse3`, `-mpopcnt`, `-march=...`, `-static`, `-Werror`, ...) and alias the
versioned GCC names some makefiles hardcode. Engines that use x86 intrinsics or
inline assembly in their *sources* still cannot build; they are listed at the
end of the run, with the full compiler output in `_build/logs/<engine>.log`.
