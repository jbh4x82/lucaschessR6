Lucas Chess R6 on macOS: build notes and gotchas
================================================

Everything that bit during the port, so it does not have to be rediscovered.
The build itself is documented in [README.md](README.md); this file is the
"why it is like that" companion.

Scripts, in the order you would use them:

| Script | Purpose |
|---|---|
| `bin/_fastercode/fastercode_macos.sh` | build the FasterCode C/Cython extension |
| `build_engines.py` | build the bundled engines from their `src.7z` archives |
| `run_tests.py` | verify a source checkout (227 checks) |
| `package_app.py` | produce `Lucas Chess R6.app` and a `.dmg` |
| `test_app_bundle.py` | verify the packaged app the way a user installs it |


Cloning
-------

**Git LFS is mandatory.** `.gitattributes` has exactly one rule, `*.nnue`, so
only four objects (107 MB of Stockfish nets) are LFS. Without `git-lfs`
installed the checkout **aborts in the middle** with
`git-lfs filter-process: git-lfs: command not found`, leaving ~3000 files staged
as deleted. It looks like a corrupt clone rather than a missing tool.

    brew install git-lfs && git lfs install

Everything else, including all the Linux and Windows engine binaries (458 MB),
is stored as ordinary git blobs, not LFS.

**Do not clone into an iCloud-synced folder.** On a Mac with Desktop &
Documents syncing on (`defaults read com.apple.finder | grep FXICloudDrive`),
`~/Documents` and `~/Desktop` are managed by iCloud: it uploads the 477 MB
packfile and can evict files from under git.


FasterCode
----------

* `src/setup_linux.py` uses `distutils`, which is gone in Python 3.12+.
  `setup_macos.py` uses setuptools instead, modelled on `setup_windows.py`.
* Drop `-march=x86-64 -mtune=generic`; clang on arm64 rejects them. Nothing in
  the irina C sources uses x86 intrinsics, so no other change is needed.
* `src/irina/util.c` needs `<sys/select.h>` and `<sys/time.h>`. glibc pulls
  those in via `<sys/types.h>`; macOS libc does not, so `select()` and
  `struct timeval` are undeclared.
* Set `MACOSX_DEPLOYMENT_TARGET` to whatever Python was built against, or the
  linker warns once per object file in `libirina.a`.
* `import FasterCode` is a plain top-level import, so the `.so` has to sit in
  `bin/`, not next to its sources.
* `FasterCode.xpgn_pv()` **segfaults** if called before a move generation:
  `pgn2pv()` in `lc.c` reads `board.ply_moves[board.ply - 1]`, which is out of
  bounds at `ply == 0`. Upstream's own test harness calls `movegen()` first.
  This is a pre-existing precondition, not a macOS problem, and the program
  itself does not use that function (it uses `xparse_pgn`).


Engines
-------

Each engine folder ships its own sources as `src.7z`, which is what makes a
native build possible at all. 18 of 27 tried build on arm64.

* These makefiles pass x86-only switches unconditionally. `build_engines.py`
  puts compiler shims first on `PATH` that strip `-msse*`, `-mpopcnt`,
  `-mavx*`, `-march=`, `-mtune=`, `-static` and `-Werror`, and alias the
  versioned GCC names a few of them hardcode (patricia wants `g++-12`).
* The shims also drop `.h` arguments: ceechess lists a header among its `.c`
  files, and clang then refuses to combine a precompiled header with `-o`.
* `weiss` needs `llvm-profdata` for its default PGO target; Xcode ships it only
  behind `xcrun`, so there is a shim for that too.
* Stockfish: build `ARCH=apple-silicon`, and its `scripts/net.sh` is **not**
  inside `src.7z`, so the build stops trying to download the nets. The recipe
  stages the two `.nnue` files from the engine folder and stubs the script.
  The result is built with `NNUE_EMBEDDING_OFF`, so both nets must sit beside
  the binary at runtime.
* The nets are symlinked to the copies under `OS/linux` rather than duplicated:
  104 MB + 3.4 MB per copy is more than everything else in the folder together.
* What still cannot build, and why: `stash`, `tucano`, `igel`, `gunborg`,
  `toga`, `greko`, `cinnamon` (x86 intrinsics, inline assembly, `immintrin.h`,
  or a prebuilt Linux `libgtb.a`), `amoeba` (needs a D compiler), `daydreamer`
  (its `src.7z` is missing `daydreamer.h`).
* Engine binaries are only ever *read* by the program, so a symlink or a
  read-only file is fine, with one exception: see `uci_options.sqlite` below.


Platform assumptions in the program
-----------------------------------

* `Util.is_linux()` was used for two different meanings. Some call sites mean
  "POSIX" (file permissions, `nice` values, the file dialog filter) and some
  mean "Linux" (the e-board `.so` drivers, the Wayland check). Added
  `is_macos()` and `is_posix()` and split the call sites. Miss this and
  `Priorities` reaches `psutil.NORMAL_PRIORITY_CLASS`, which exists only on
  Windows.
* `EngineRun` sets `LD_LIBRARY_PATH`; macOS needs `DYLD_LIBRARY_PATH`.
* `font_mono` was `"Mono"` for anything non-Windows. macOS has no such family,
  so Qt spends time resolving aliases and warns. Menlo is the equivalent.
* `CheckEngines` picks a Stockfish binary matching the CPU's flags from a family
  of x86 builds. On arm64 there is one native build, and the original code
  **deletes the target first** and then fails to copy an x86 replacement, so it
  silently removes the working engine. It now exits early off
  `platform.machine()`.
* `Configuration.dic_books` scans a fixed list of engine folders for `.bin`
  books, including `maia` and `rodentii`, neither of which builds for arm64.
  `os.scandir` on the missing folder raised and took down the "Play against an
  engine" dialog. It now skips folders that are not installed, `path_book`
  returns `None` instead of raising, and `EnginesWicker` checks that an engine
  *and* its book exist before offering it. 39 of the ~150 wicker opponents
  survive on macOS, elo 171 to 2795; the rest are Rodent II personalities.


Packaging the .app
------------------

The app ships the program as *data* (`bin/` and `Resources/`, laid out as in a
checkout) and only freezes the interpreter and the third-party libraries.
`app_launcher.py` then sets `sys.argv[0]`, `chdir`s and hands over to
`LucasR.py` through `runpy`, so upstream's argument handling (`.pgn`,
`-tournament`, `-kibitzer`, ...) is not duplicated anywhere.

The gotchas, all of which cost a build cycle:

* **PyInstaller sees none of the program's imports**, because the source is
  data. It freezes neither the third-party packages nor the parts of the
  standard library the program uses. `wave` was the one that finally made this
  obvious. `package_app.py` now parses every `.py` with `ast` and passes the
  discovered module names as hidden imports, so the list cannot drift.
* Submodules imported lazily still need `collect_submodules`: the endgame
  trainers reach `chess.gaviota` and `chess.syzygy` from deep inside a menu, so
  a top-level `chess` hidden import is not enough.
* `OS/darwin/OSEngines.py` reads the engine catalogue out of
  `OS/linux/OSEngines.py`. That one file has to be staged even though no Linux
  engine binary ships.
* The staging copy must **dereference symlinks** (`copytree(symlinks=False)`),
  or the app carries dangling links where the NNUE nets should be.
* PyInstaller copies data files **without the executable bit**, so every engine
  binary has to be `chmod +x`ed again after the build.
* On Apple silicon every executable needs at least an ad-hoc signature. Sign
  the engines individually first, then `--deep` sign the bundle.
* PyInstaller puts data in `Contents/Resources` and links it into
  `Contents/Frameworks`, one symlink per top-level entry. Two consequences:
  summing `Path.rglob()` sizes double-counts (the app is 522 MB, not 885 MB),
  and `rglob` **does not descend into a symlinked directory** - which silently
  made the bundle self-test walk zero modules and report success.
* The installed bundle is read-only, so two things are redirected: user data to
  `~/Library/Application Support/Lucas Chess R6`, and `bug.log`, which
  `Code.Main.Init` opens by a relative path, into the same folder.
* `Engines.read_uci_options()` **writes** its cache to
  `OS/darwin/uci_options.sqlite` on a miss, inside the bundle. `package_app.py`
  primes that cache at build time so the installed app never needs to.
* `Analysis(optimize=1)` does not reach the frozen interpreter's flags:
  `__debug__` stays true and the program takes its debug logging path. Left
  alone rather than kept as a setting that does nothing.
* Excluding the Qt modules the program does not use matters: the app needs only
  QtCore, QtGui, QtWidgets, QtMultimedia, QtSvg and QtSvgWidgets. PySide6
  installs 1.1 GB; the finished disk image is 276 MB.


Distribution
------------

`package_app.py` signs with a **Developer ID Application** certificate and
notarizes when one is installed, and falls back to an ad-hoc signature when it
is not. Set it up once:

1. Create the certificate: Xcode > Settings > Accounts > your Apple ID >
   Manage Certificates > + > **Developer ID Application**. An "Apple
   Development" certificate is *not* usable here; it cannot be notarized.
2. Create an app-specific password at appleid.apple.com > Sign-In and Security,
   then store it for `notarytool`:

       xcrun notarytool store-credentials "lucaschess" \
           --apple-id <your-apple-id> --team-id QVF7W32W9J --password <app-specific-password>

After that `package_app.py` signs every Mach-O in the bundle with the hardened
runtime and a secure timestamp, submits the app, staples the ticket, builds the
disk image, signs and notarizes that too, and prints the Gatekeeper verdict for
both. `--no-notarize` signs without submitting; `--identity` picks a specific
certificate.

The hardened runtime needs entitlements a frozen CPython cannot do without:
`allow-jit` and `allow-unsigned-executable-memory` (it generates code at
runtime), `disable-library-validation` (it dlopen()s extension modules), and
`allow-dyld-environment-variables` (EngineRun passes `DYLD_LIBRARY_PATH` to
engines). Sign inner binaries **before** the enclosing bundle, deepest first, or
the outer signature seals a stale hash.

### Without a Developer ID certificate

The ad-hoc fallback still runs, but users meet Gatekeeper. Consequences, all
expected:

* `spctl -a --type execute` reports `rejected`.
* A download carries `com.apple.quarantine`, and the copy dragged to
  Applications inherits it, so the first open is refused with "Apple could not
  verify this app is free of malware".
* **macOS 15 removed the Control-click > Open bypass.** On Sequoia and later the
  only route is: click Done, then System Settings > Privacy & Security > scroll
  to Security > "Open Anyway", then authenticate. The dialog itself offers only
  "Done" and "Move to Bin", which reads like a dead end, so say this plainly
  anywhere the download is offered. `xattr -dr com.apple.quarantine <app>` is
  the equivalent from a terminal.
* `test_app_bundle.py` reproduces that path: it quarantines the image the way
  Safari would, mounts it, installs to `/Applications`, reports the Gatekeeper
  verdict, then clears the flag the way the user's approval does, and only then
  checks that the app runs, that the bundled engines play, and that nothing was
  written inside the bundle. Note that clearing the flag is what the *approval*
  does; the test deliberately does not assert the app opens while quarantined,
  because on macOS 15+ it will not.
