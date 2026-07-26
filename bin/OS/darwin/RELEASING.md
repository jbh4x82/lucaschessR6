Picking up a new Lucas Chess release
====================================

Upstream tags a release roughly every few weeks (`Version R6.0.x`). This is the
whole process for turning one of those into a new macOS disk image. Everything
except the two Apple account steps is scripted; a routine update is about
twenty minutes, most of it waiting for Apple.

The macOS work lives on the `macos-arm64` branch of the fork, as a small set of
commits on top of upstream's `main`. Keeping it that way, rather than letting
the branch drift, is what makes updates cheap: `git rebase` replays a handful of
patches onto the new release.


1. Bring in the new upstream release
------------------------------------

    cd ~/GitHub/lucaschessR6
    git remote -v                       # origin = lukasmonk, fork = jbh4x82
    git fetch origin

    git checkout macos-arm64
    git rebase origin/main

Conflicts, if any, will be in the handful of files the port touches:
`bin/Code/__init__.py`, `Code/Z/Util.py`, `Code/Engines/{EngineRun,CheckEngines,
EnginesWicker}.py`, `Code/Config/Configuration.py`,
`bin/_fastercode/src/irina/util.c`. Take upstream's version and re-apply the
macOS change; [BUILD-NOTES.md](BUILD-NOTES.md) explains what each one is for and
why, so none of it has to be reverse-engineered again.

Then check whether upstream changed anything the port depends on:

    git diff origin/main@{1} origin/main --stat -- bin/OS/linux/OSEngines.py \
        bin/OS/linux/Engines bin/Code/Engines requirements.txt

* **`OS/linux/OSEngines.py`** matters most: `OS/darwin/OSEngines.py` reuses that
  catalogue, so a renamed engine binary or a new engine shows up here.
* **New or updated engines** need a recipe in `build_engines.py`, keyed by the
  file name the catalogue expects. `python3 build_engines.py --list` shows the
  current ones.
* **`requirements.txt`**: if a dependency is added, `package_app.py` discovers
  it automatically from the source, but the venv needs updating.


2. Rebuild
----------

    .venv/bin/pip install -r requirements.txt cython pyinstaller
    bin/_fastercode/fastercode_macos.sh          # only if _fastercode changed
    python3 bin/OS/darwin/build_engines.py       # only if engines changed

`build_engines.py` reports which engines built and which did not, with a log per
failure in `_build/logs/`. A newly failing engine is not a blocker: the
catalogue only registers what is present.


3. Test the source tree
-----------------------

    .venv/bin/python bin/OS/darwin/run_tests.py

227 checks: FasterCode against python-chess, every installed engine over UCI,
the app's configuration, and an offscreen render. **Fix anything red before
packaging** - it is much cheaper to diagnose here than inside a bundle.


4. Build, sign and notarize the app
-----------------------------------

    .venv/bin/python bin/OS/darwin/package_app.py

This stages the tree, primes the UCI cache, freezes with PyInstaller, signs
every Mach-O with the Developer ID certificate and the hardened runtime,
notarizes the app, staples it, builds the disk image, notarizes and staples that
too, and prints the Gatekeeper verdict for both. Expect `source=Notarized
Developer ID`.

It needs the certificate and the `lucaschess` notarytool profile to be in the
keychain; see the Distribution section of [BUILD-NOTES.md](BUILD-NOTES.md) for
how those were set up. Without them it falls back to an ad-hoc signature and
says so.


5. Test the installer as a user meets it
----------------------------------------

    python3 bin/OS/darwin/test_app_bundle.py

Quarantines the image the way a browser does, mounts it, installs to
`/Applications`, makes the frozen bundle import all its own modules, launches it
twice, drives the bundled engines, checks nothing is written inside the bundle,
then uninstalls. 28 checks. This is the step that catches packaging bugs the
source-tree tests cannot see.


6. Publish
----------

    python3 bin/OS/darwin/publish.py

Uploads the image over FTPS, regenerates the download page with the new size and
checksum, picks the install instructions that match how the build is actually
signed, and then downloads the hosted copy back and compares the hash. Bump
`VERSION` in `publish.py` when upstream's version changes.


7. Update the places that quote the release
-------------------------------------------

- `readme.md` on the fork: the download link mentions the size.
- `bin/OS/darwin/README.md`: same.
- The comment on
  [lukasmonk/lucaschessR6#8](https://github.com/lukasmonk/lucaschessR6/issues/8),
  if the engine list or the install steps changed.
- Push the branch: `git push --force-with-lease fork macos-arm64` (force is
  expected, since step 1 rebased).


Checklist
---------

    [ ] git fetch origin && git rebase origin/main
    [ ] reviewed changes to OS/linux/OSEngines.py, engines, requirements.txt
    [ ] rebuilt FasterCode and engines if their sources changed
    [ ] run_tests.py green
    [ ] package_app.py reports "Notarized Developer ID" for app and dmg
    [ ] test_app_bundle.py green
    [ ] publish.py verified the hosted copy
    [ ] readme download size and upstream comment updated
    [ ] git push --force-with-lease fork macos-arm64
