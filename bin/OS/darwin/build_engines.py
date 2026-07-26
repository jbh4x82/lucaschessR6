#!/usr/bin/env python3
"""Build the bundled chess engines for macOS and install them under OS/darwin/Engines.

Every engine folder in OS/linux/Engines ships its own sources as src.7z. This
driver extracts them, builds with clang for the host architecture, and installs
the resulting binary - under the same file name the shared engine catalogue
expects - into OS/darwin/Engines/<engine>/, together with the data files the
engine needs at runtime (NNUE nets, opening books, personality files...).

Most of these makefiles assume gcc on x86 Linux, so builds run with a set of
compiler shims first on PATH (see _write_shims) that drop x86-only switches
(-msse3, -mpopcnt, -march=..., -static, ...) before calling the real compiler.
Engines that need x86 intrinsics or assembly in their sources still fail; they
are reported at the end and simply stay unavailable. OSEngines.py registers only
the engines actually present, so a partial result is perfectly usable.

Usage:
    python3 build_engines.py                 # build everything we have a recipe for
    python3 build_engines.py stockfish irina # build only these
    python3 build_engines.py --list          # show the recipes
    python3 build_engines.py --clean         # remove the scratch build tree
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent  # bin/OS/darwin
OS_DIR = HERE.parent  # bin/OS
LINUX_ENGINES = OS_DIR / "linux" / "Engines"
DEST_ENGINES = HERE / "Engines"
BUILD_ROOT = HERE / "_build"
SHIM_DIR = BUILD_ROOT / "shims"
ARCH = os.uname().machine  # arm64 / x86_64

SEVENZIP = shutil.which("7zz") or shutil.which("7z") or shutil.which("7za")

ELF_MAGIC = b"\x7fELF"

# Data files at least this big are symlinked to the copy under OS/linux instead
# of being duplicated (see install_data_files).
LINK_INSTEAD_OF_COPY_BYTES = 1024 * 1024


@dataclass
class Recipe:
    """How to build one engine for macOS.

    make_dir:    directory inside the extracted archive holding the makefile
    make_target: make target (empty for the makefile's default)
    make_args:   extra arguments / variable overrides passed to make
    built:       name of the binary make produces, relative to make_dir
    exe:         file name the binary must have in OS/darwin/Engines/<key>/;
                 this has to match the name in OS/linux/OSEngines.py, which is
                 the catalogue OS/darwin/OSEngines.py reuses
    note:        shown by --list and in the final report
    """

    key: str
    exe: str
    built: str
    make_dir: str = "src"
    make_target: str = ""
    make_args: list[str] = field(default_factory=list)
    note: str = ""


RECIPES: list[Recipe] = [
    Recipe(
        "irina",
        exe="irina",
        built="irina",
        make_args=["CFLAGS=-std=gnu17 -O2 -DNDEBUG", "LDFLAGS="],
        note="Lucas Monge's own engine, same C base as FasterCode",
    ),
    Recipe(
        "stockfish",
        exe="stockfish-18-64",
        built="stockfish",
        make_target="build",
        make_args=[f"ARCH={'apple-silicon' if ARCH == 'arm64' else 'x86-64-bmi2'}", "COMP=clang"],
        note="needs both NNUE nets beside the binary (built with NNUE_EMBEDDING_OFF)",
    ),
    Recipe("fruit", exe="Fruit-2.1", built="fruit"),
    Recipe("gambitfruit", exe="gfruit", built="gfruit"),
    Recipe("delocto", exe="Delocto-0.61n", built="Delocto"),
    Recipe("k2", exe="K2-0.99", built="k2_0992dev", make_dir="k2-master"),
    Recipe("wyldchess", exe="WyldChess-1.51", built="wyldchess"),
    Recipe("patricia", exe="patricia_4_v2", built="patricia", make_dir="engine"),
    Recipe("greko98", exe="GreKo98a", built="greko"),
    Recipe("glaurung", exe="Glaurung-2.2", built="glaurung"),
    Recipe("ethereal", exe="Ethereal-12.75", built="Ethereal"),
    Recipe("laser", exe="Laser-1.17", built="laser"),
    Recipe("igel", exe="Igel-3.0.10", built="igel"),
    Recipe("beef", exe="Beef-0.36", built="beef"),
    Recipe("stash", exe="Stash-29.0", built="stash-bot"),
    Recipe("monolith", exe="Monolith-2.01", built="Monolith.exe", make_dir="Source"),
    Recipe("eguzki", exe="eguzki", built="eguzki.exe"),
    Recipe("eguzkilore", exe="eguzkilore", built="eguzkilore.exe"),
    Recipe("weiss", exe="Weiss-1.2", built="weiss", make_target="basic"),
    Recipe("tucano", exe="Tucano-9.00", built="tucano"),
    Recipe("gunborg", exe="Gunborg-1.35", built="gunborg"),
    Recipe("ceechess", exe="CeeChess-1.3.2", built="CeeChess-v1.3.2.exe", make_target="linux"),
    Recipe("toga", exe="DeepToga1.9.6nps", built="toga"),
    Recipe("greko", exe="GreKo-2020.03", built="greko"),
    Recipe(
        "cinnamon",
        exe="Cinnamon-1.2b",
        built="cinnamon",
        make_target="cinnamon64-generic",
        note="links a prebuilt Linux libgtb.a, so this normally fails",
    ),
    Recipe(
        "amoeba",
        exe="Amoeba-2.6",
        built="amoeba",
        make_args=["DC=ldc2"],
        note="written in D, needs a D compiler (brew install ldc)",
    ),
    Recipe("daydreamer", exe="Daydreamer-1.75", built="daydreamer"),
]

RECIPES_BY_KEY = {r.key: r for r in RECIPES}

# Switches these makefiles pass for x86 Linux that clang rejects (or that make
# no sense) when targeting macOS. Prefixes are matched with startswith.
DROP_EXACT = {
    "-static",
    "-static-libgcc",
    "-static-libstdc++",
    "-Werror",
    "-fprefetch-loop-arrays",
    "-m32",
    "-fno-strict-overflow",
    "-Wl,--no-as-needed",
    "-no-pie",
}
DROP_PREFIX = (
    "-msse",
    "-mno-sse",
    "-mpopcnt",
    "-mno-popcnt",
    "-mavx",
    "-mbmi",
    "-mfma",
    "-mlzcnt",
    "-mabm",
    "-mfpmath=",
    "-march=",
    "-mtune=",
    "-mprefer-",
    "-mssse",
    "-msse4a",
    "-mcx16",
    "-mstackrealign",
)

SHIM_TEMPLATE = """#!/usr/bin/env python3
# Compiler shim used by build_engines.py: drops x86-only / Linux-only switches
# that clang cannot accept when targeting macOS, then calls the real compiler.
import os
import sys

REAL = {real!r}
DROP_EXACT = {drop_exact!r}
DROP_PREFIX = {drop_prefix!r}

args = []
for arg in sys.argv[1:]:
    if arg in DROP_EXACT or arg.startswith(DROP_PREFIX):
        continue
    # A couple of makefiles list headers alongside the .c files; clang would try
    # to precompile them and then refuse to combine that with -o.
    if arg.endswith((".h", ".hpp")):
        continue
    args.append(arg)
os.execv(REAL, [REAL] + args)
"""


def _write_shims() -> dict[str, str]:
    """Create the shim directory and return the environment builds should use."""
    SHIM_DIR.mkdir(parents=True, exist_ok=True)

    real_cc = shutil.which("clang", path="/usr/bin:/usr/local/bin") or "/usr/bin/clang"
    real_cxx = shutil.which("clang++", path="/usr/bin:/usr/local/bin") or "/usr/bin/clang++"

    # Some makefiles hardcode a versioned GCC (patricia wants g++-12), so shim
    # those names too.
    versions = range(9, 16)
    c_names = ["clang", "gcc", "cc"] + [f"gcc-{v}" for v in versions]
    cxx_names = ["clang++", "g++", "c++"] + [f"g++-{v}" for v in versions]

    for names, real in ((c_names, real_cc), (cxx_names, real_cxx)):
        body = SHIM_TEMPLATE.format(
            real=real,
            drop_exact=sorted(DROP_EXACT),
            drop_prefix=DROP_PREFIX,
        )
        for name in names:
            shim = SHIM_DIR / name
            shim.write_text(body)
            shim.chmod(0o755)

    # Some makefiles run llvm-profdata for their PGO build; Xcode ships it but
    # only behind xcrun.
    profdata = SHIM_DIR / "llvm-profdata"
    profdata.write_text('#!/bin/sh\nexec xcrun llvm-profdata "$@"\n')
    profdata.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{SHIM_DIR}:{env.get('PATH', '')}"
    return env


def log(msg: str) -> None:
    print(msg, flush=True)


def run(cmd: list[str], cwd: Path, logfile: Path, env: dict[str, str] | None = None) -> bool:
    with logfile.open("ab") as fh:
        fh.write(f"\n$ {' '.join(cmd)}\n".encode())
        fh.flush()
        proc = subprocess.run(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, env=env)
    return proc.returncode == 0


def extract(recipe: Recipe, logfile: Path) -> Path | None:
    archive = LINUX_ENGINES / recipe.key / "src.7z"
    if not archive.is_file():
        return None
    workdir = BUILD_ROOT / recipe.key
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    if not run([SEVENZIP, "x", "-y", str(archive)], workdir, logfile):
        return None
    return workdir


def stage_stockfish_nets(workdir: Path, make_dir: Path) -> None:
    """Stockfish builds with NNUE_EMBEDDING_OFF and its scripts/net.sh - which
    would download the nets - is not inside src.7z, so stage the nets shipped
    with the engine and stub the script out."""
    for net in ("nn-c288c895ea92.nnue", "nn-37f18f62d772.nnue"):
        src = LINUX_ENGINES / "stockfish" / net
        if src.is_file():
            shutil.copy2(src, make_dir / net)
    scripts = make_dir.parent / "scripts"
    scripts.mkdir(exist_ok=True)
    net_sh = scripts / "net.sh"
    net_sh.write_text(
        "#!/bin/sh\n"
        "# Stub for upstream scripts/net.sh: the nets are staged from the engine\n"
        "# folder before the build, so there is nothing to download.\n"
        'echo "Networks staged locally, skipping download."\n'
    )
    net_sh.chmod(0o755)


PRE_BUILD_HOOKS = {"stockfish": stage_stockfish_nets}


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(4) == ELF_MAGIC
    except OSError:
        return False


def install_data_files(recipe: Recipe, dest_dir: Path) -> None:
    """Copy the engine's runtime data (books, nets, personality files) and its
    licence/readme from the Linux folder, skipping the Linux binaries and the
    source archives.

    Large payloads are symlinked rather than copied: Stockfish's big NNUE net
    alone is 104 MB and is already in the repository under OS/linux, so
    duplicating it would more than double what this folder adds to a checkout.
    The links are relative, so they survive the repo being moved or cloned."""
    source_dir = LINUX_ENGINES / recipe.key
    for entry in sorted(source_dir.iterdir()):
        if entry.suffix.lower() in (".7z", ".exe", ".dll", ".so"):
            continue
        if entry.is_dir():
            shutil.copytree(entry, dest_dir / entry.name, dirs_exist_ok=True)
        elif entry.is_file() and not _is_elf(entry):
            dest = dest_dir / entry.name
            if dest.is_symlink() or dest.exists():
                dest.unlink()
            if entry.stat().st_size >= LINK_INSTEAD_OF_COPY_BYTES:
                dest.symlink_to(os.path.relpath(entry, dest_dir))
            else:
                shutil.copy2(entry, dest)


def build_one(recipe: Recipe, env: dict[str, str]) -> tuple[bool, str]:
    logdir = BUILD_ROOT / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{recipe.key}.log"
    logfile.write_bytes(b"")

    workdir = extract(recipe, logfile)
    if workdir is None:
        return False, "no src.7z (or extraction failed)"

    make_dir = workdir / recipe.make_dir
    if not make_dir.is_dir():
        # Some archives nest one level deeper than the others.
        candidates = [p for p in workdir.iterdir() if p.is_dir()]
        if len(candidates) == 1 and (candidates[0] / recipe.make_dir).is_dir():
            make_dir = candidates[0] / recipe.make_dir
        else:
            return False, f"no {recipe.make_dir}/ in archive"

    hook = PRE_BUILD_HOOKS.get(recipe.key)
    if hook:
        hook(workdir, make_dir)

    cmd = ["make", "-j", str(os.cpu_count() or 4)]
    if recipe.make_target:
        cmd.append(recipe.make_target)
    cmd += recipe.make_args
    if not run(cmd, make_dir, logfile, env=env):
        return False, f"make failed (log: {logfile.relative_to(HERE)})"

    built = make_dir / recipe.built
    if not built.is_file():
        return False, f"make succeeded but {recipe.built} was not produced"

    dest_dir = DEST_ENGINES / recipe.key
    dest_dir.mkdir(parents=True, exist_ok=True)
    install_data_files(recipe, dest_dir)

    dest = dest_dir / recipe.exe
    shutil.copy2(built, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return True, str(dest.relative_to(HERE))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("engines", nargs="*", help="engine keys to build (default: all recipes)")
    parser.add_argument("--list", action="store_true", help="list the known recipes and exit")
    parser.add_argument("--clean", action="store_true", help="delete the scratch build tree and exit")
    args = parser.parse_args()

    if args.list:
        for r in RECIPES:
            log(f"{r.key:14s} -> Engines/{r.key}/{r.exe}" + (f"   # {r.note}" if r.note else ""))
        return 0

    if args.clean:
        if BUILD_ROOT.exists():
            shutil.rmtree(BUILD_ROOT)
            log(f"removed {BUILD_ROOT}")
        return 0

    if SEVENZIP is None:
        log("error: need 7-Zip to unpack the engine sources -> brew install sevenzip")
        return 1
    if not LINUX_ENGINES.is_dir():
        log(f"error: {LINUX_ENGINES} not found (is Git LFS installed and the repo fully checked out?)")
        return 1

    keys = args.engines or [r.key for r in RECIPES]
    unknown = [k for k in keys if k not in RECIPES_BY_KEY]
    if unknown:
        log(f"error: no recipe for {', '.join(unknown)} (see --list)")
        return 1

    env = _write_shims()

    log(f":: Building {len(keys)} engine(s) for macOS/{ARCH}")
    ok, failed = [], []
    for key in keys:
        started = time.time()
        success, detail = build_one(RECIPES_BY_KEY[key], env)
        elapsed = time.time() - started
        if success:
            ok.append(key)
            log(f"   {key:14s} OK ({elapsed:.0f}s) -> {detail}")
        else:
            failed.append((key, detail))
            log(f"   {key:14s} skipped - {detail}")

    log("")
    log(f":: Built {len(ok)}/{len(keys)}: {', '.join(ok) if ok else '-'}")
    if failed:
        log(":: Not available on this machine:")
        for key, detail in failed:
            note = RECIPES_BY_KEY[key].note
            log(f"   {key:14s} {detail}" + (f" [{note}]" if note else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
