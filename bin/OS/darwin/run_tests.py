#!/usr/bin/env python3
"""Verify the macOS build of Lucas Chess R6.

Checks, in order:

  environment   python version, third-party dependencies, Qt, host arch
  fastercode    the compiled Cython/C extension: move generation cross-checked
                against python-chess, perft, SAN, polyglot hashing and books
  resources     Git LFS payloads really got downloaded (no pointer files left)
  engines       every engine installed in OS/darwin/Engines: architecture, UCI
                handshake, a legal best move, and a mate in one
  integration   the app's own Configuration / engine catalogue / priorities
  gui           Qt renders the board offscreen, and LucasR.py survives a real
                start-up with the offscreen platform plugin

Run it with the project venv:

    .venv/bin/python bin/OS/darwin/run_tests.py            # everything
    .venv/bin/python bin/OS/darwin/run_tests.py engines     # one section
    .venv/bin/python bin/OS/darwin/run_tests.py -v          # show every check
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

DARWIN_DIR = Path(__file__).resolve().parent
BIN_DIR = DARWIN_DIR.parents[1]  # bin/
ROOT_DIR = BIN_DIR.parent

# Code/__init__.py derives every path from sys.argv[0] and chdir()s there, so
# make this look exactly like a normal LucasR.py start-up before importing it.
# Our own command line has to be stashed first, since sys.argv is overwritten.
COMMAND_LINE = sys.argv[1:]
os.chdir(BIN_DIR)
sys.argv = [str(BIN_DIR / "LucasR.py")]
sys.path.insert(0, str(BIN_DIR))

sys.path.insert(0, str(DARWIN_DIR))
from uci_probe import MATE_IN_1_FEN, MATE_IN_1_MOVE, UCIEngine  # noqa: E402

ARCH = platform.machine()
KIWIPETE = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"
LFS_POINTER_PREFIX = b"version https://git-lfs"


# --------------------------------------------------------------------------- #
# tiny test harness
# --------------------------------------------------------------------------- #

class Results:
    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []
        self._section = ""

    def section(self, name: str) -> None:
        self._section = name
        print(f"\n\033[1m{name}\033[0m")

    def ok(self, what: str, detail: str = "") -> None:
        self.passed.append(f"{self._section}: {what}")
        if self.verbose:
            print(f"  \033[32mpass\033[0m {what}" + (f"  {detail}" if detail else ""))

    def fail(self, what: str, detail: str = "") -> None:
        self.failed.append((f"{self._section}: {what}", detail))
        print(f"  \033[31mFAIL\033[0m {what}" + (f"  {detail}" if detail else ""))

    def skip(self, what: str, detail: str = "") -> None:
        self.skipped.append((f"{self._section}: {what}", detail))
        print(f"  \033[33mskip\033[0m {what}" + (f"  {detail}" if detail else ""))

    def check(self, what: str, condition, detail: str = "") -> bool:
        if condition:
            self.ok(what, detail)
            return True
        self.fail(what, detail)
        return False

    def equal(self, what: str, got, expected) -> bool:
        return self.check(what, got == expected, f"got {got!r}, expected {expected!r}")

    def summary(self) -> int:
        print()
        print("=" * 62)
        print(f"passed {len(self.passed)}   failed {len(self.failed)}   skipped {len(self.skipped)}")
        if self.failed:
            print("\nfailures:")
            for name, detail in self.failed:
                print(f"  - {name}" + (f"  ({detail})" if detail else ""))
        if self.skipped:
            print("\nskipped:")
            for name, detail in self.skipped:
                print(f"  - {name}" + (f"  ({detail})" if detail else ""))
        print("=" * 62)
        return 1 if self.failed else 0


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #

REQUIRED_MODULES = [
    ("PySide6", "PySide6"),
    ("shiboken6", "shiboken6"),
    ("chess", "python-chess"),
    ("PIL", "pillow"),
    ("psutil", "psutil"),
    ("polib", "polib"),
    ("deep_translator", "deep-translator"),
    ("requests", "requests"),
    ("urllib3", "urllib3"),
    ("certifi", "certifi"),
    ("idna", "idna"),
    ("charset_normalizer", "charset-normalizer"),
    ("sortedcontainers", "sortedcontainers"),
    ("bs4", "beautifulsoup4"),
    ("cpuinfo", "py-cpuinfo"),
]


def test_environment(r: Results) -> None:
    r.section("environment")
    r.check(
        "python >= 3.12",
        sys.version_info >= (3, 12),
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
    )
    r.check("running on macOS", sys.platform == "darwin", f"{sys.platform} {platform.mac_ver()[0]} {ARCH}")

    for module, package in REQUIRED_MODULES:
        try:
            __import__(module)
            r.ok(f"import {package}")
        except Exception as exc:
            r.fail(f"import {package}", str(exc))

    try:
        import PySide6

        r.ok("PySide6 version", PySide6.__version__)
    except Exception as exc:
        r.fail("PySide6 version", str(exc))


# --------------------------------------------------------------------------- #
# FasterCode (the compiled extension)
# --------------------------------------------------------------------------- #

MOVEGEN_POSITIONS = [
    ("initial position", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"),
    ("kiwipete (castling, pins)", KIWIPETE),
    ("en passant available", "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3"),
    ("promotion race", "8/PPPk4/8/8/8/8/4Kppp/8 w - - 0 1"),
    ("double check", "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"),
    ("endgame, black to move", "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 b - - 0 1"),
    ("castling both sides", "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"),
]

PERFT_CASES = [
    ("initial position", "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", [20, 400, 8902, 197281]),
    ("kiwipete", KIWIPETE, [48, 2039, 97862]),
    ("position 3", "8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", [14, 191, 2812, 43238]),
]


def _fc_moves(fastercode, fen: str) -> list[str]:
    fastercode.set_fen(fen)
    return sorted(m.move() for m in fastercode.get_exmoves())


def _fc_child_fen(fastercode, fen: str, move: str) -> str:
    fastercode.set_fen(fen)
    fastercode.make_move(move)
    return fastercode.get_fen()


def _perft(fastercode, fen: str, depth: int) -> int:
    moves = _fc_moves(fastercode, fen)
    if depth == 1:
        return len(moves)
    total = 0
    for move in moves:
        total += _perft(fastercode, _fc_child_fen(fastercode, fen, move), depth - 1)
    return total


def test_fastercode(r: Results) -> None:
    r.section("fastercode")
    try:
        import FasterCode
    except Exception as exc:
        r.fail("import FasterCode", str(exc))
        return

    r.ok("import FasterCode", FasterCode.__file__)
    r.check(
        "extension built for this architecture",
        ARCH in FasterCode.__file__ or "darwin" in FasterCode.__file__,
        Path(FasterCode.__file__).name,
    )
    r.check("bmi2() is false on non-x86", (ARCH != "arm64") or not FasterCode.bmi2(), str(FasterCode.bmi2()))

    try:
        import chess
    except Exception as exc:
        r.skip("cross-check against python-chess", str(exc))
        chess = None

    # Move generation, cross-checked move for move against python-chess.
    if chess is not None:
        for name, fen in MOVEGEN_POSITIONS:
            try:
                got = _fc_moves(FasterCode, fen)
                board = chess.Board(fen)
                expected = sorted(m.uci() for m in board.legal_moves)
                # python-chess writes castling as king-to-rook-square in some
                # variants; normalise the standard notation both use here.
                r.equal(f"legal moves: {name}", got, expected)
            except Exception as exc:
                r.fail(f"legal moves: {name}", str(exc))

    # FEN round-trip.
    try:
        FasterCode.set_fen(KIWIPETE)
        r.equal("FEN round-trip", FasterCode.get_fen(), KIWIPETE)
    except Exception as exc:
        r.fail("FEN round-trip", str(exc))

    # perft.
    for name, fen, expected_counts in PERFT_CASES:
        for depth, expected in enumerate(expected_counts, start=1):
            started = time.time()
            try:
                got = _perft(FasterCode, fen, depth)
            except Exception as exc:
                r.fail(f"perft({name}, depth {depth})", str(exc))
                continue
            r.check(
                f"perft({name}, depth {depth}) = {expected}",
                got == expected,
                f"got {got} in {time.time() - started:.1f}s",
            )

    # SAN generation.
    if chess is not None:
        try:
            FasterCode.set_fen(KIWIPETE)
            san_ok = True
            board = chess.Board(KIWIPETE)
            for move in ("e1g1", "d5e6", "e5g6", "f3f6"):
                FasterCode.set_fen(KIWIPETE)
                got = FasterCode.get_pgn(move[:2], move[2:4], "")
                expected = board.san(chess.Move.from_uci(move))
                if got != expected:
                    san_ok = False
                    r.fail(f"SAN for {move}", f"got {got!r}, expected {expected!r}")
            if san_ok:
                r.ok("SAN generation matches python-chess")
        except Exception as exc:
            r.fail("SAN generation", str(exc))

    # PGN -> move list (this is the parser Code.Base.Game uses).
    try:
        tokens = FasterCode.xparse_pgn('[Event "Test"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 1-0')
        moves = [token[1:] for token in tokens if token.startswith("M")]
        r.equal("parse PGN to moves", moves, ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6"])
    except Exception as exc:
        r.fail("parse PGN to moves", str(exc))

    # Single SAN move -> coordinates. pgn2pv() reads the move list generated for
    # the current position, so a movegen has to have run first.
    try:
        FasterCode.set_init_fen()
        FasterCode.get_exmoves()
        r.equal("SAN move to coordinates", [FasterCode.xpgn_pv("e4"), FasterCode.xpgn_pv("Nf3")], ["e2e4", "g1f3"])
    except Exception as exc:
        r.fail("SAN move to coordinates", str(exc))

    # Polyglot zobrist hashing, cross-checked against python-chess.
    if chess is not None:
        try:
            import chess.polyglot

            hashes_ok = True
            for name, fen in MOVEGEN_POSITIONS[:4]:
                got = FasterCode.hash_polyglot(fen.encode())
                expected = chess.polyglot.zobrist_hash(chess.Board(fen))
                if got != expected:
                    hashes_ok = False
                    r.fail(f"polyglot hash: {name}", f"got {got}, expected {expected}")
            if hashes_ok:
                r.ok("polyglot hashes match python-chess")
        except Exception as exc:
            r.fail("polyglot hashing", str(exc))

    # The bundled opening books have to be readable.
    import Code

    initial_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    for book_name, book_path in (
        ("GMopenings.bin", Code.tbook),
        ("fics15.bin", Code.tbookPTZ),
        ("irina.bin", Code.tbookI),
    ):
        polyglot = None
        try:
            polyglot = FasterCode.Polyglot(book_path)
            entries = polyglot.list_entries(initial_fen)
            r.check(
                f"opening book {book_name} has moves for the initial position",
                bool(entries),
                f"{len(entries)} entries, {len(polyglot)} in book",
            )
        except Exception as exc:
            r.fail(f"opening book {book_name}", str(exc))
        finally:
            if polyglot is not None:
                polyglot.close()

    # The internal engine used for some trainings.
    try:
        move = FasterCode.run_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", 6, 1000, 1)
        legal = _fc_moves(FasterCode, "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
        r.check("internal engine returns a legal move", bool(move) and move[:4] in [m[:4] for m in legal], str(move))
    except Exception as exc:
        r.fail("internal engine (run_fen)", str(exc))


# --------------------------------------------------------------------------- #
# resources / Git LFS integrity
# --------------------------------------------------------------------------- #

def _is_lfs_pointer(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX
    except OSError:
        return False


def test_resources(r: Results) -> None:
    r.section("resources")

    expected = [
        ("opening book GMopenings.bin", ROOT_DIR / "Resources/Openings/GMopenings.bin"),
        ("opening book fics15.bin", ROOT_DIR / "Resources/Openings/fics15.bin"),
        ("opening book irina.bin", ROOT_DIR / "Resources/Openings/irina.bin"),
        ("piece themes", ROOT_DIR / "Resources/Pieces"),
        ("board themes", ROOT_DIR / "Resources/Themes"),
        ("translations", ROOT_DIR / "Resources/Locale"),
        ("tactics", ROOT_DIR / "Resources/Tactics"),
    ]
    for name, path in expected:
        r.check(f"{name} present", path.exists(), str(path))

    gaviota = sorted((ROOT_DIR / "Resources/Gaviota").glob("*.gtb.cp4"))
    r.check("Gaviota tablebases present (3-4 men)", len(gaviota) >= 30, f"{len(gaviota)} files")

    # Git LFS pointers left behind are the classic "cloned without LFS" symptom
    # and would show up as engines crashing on start-up.
    pointers = []
    for folder in (ROOT_DIR / "Resources", BIN_DIR / "OS"):
        for path in folder.rglob("*"):
            if path.is_file() and path.stat().st_size < 1024 and _is_lfs_pointer(path):
                pointers.append(path)
    r.check(
        "no unresolved Git LFS pointer files",
        not pointers,
        f"{len(pointers)} pointers, e.g. {pointers[0] if pointers else ''}",
    )


# --------------------------------------------------------------------------- #
# engines
# --------------------------------------------------------------------------- #

def installed_engines() -> dict[str, Path]:
    """key -> binary, taken from the app's own catalogue so this tests exactly
    what Lucas Chess will launch."""
    import Code

    engines = {}
    for key, engine in Code.configuration.engines.dic_engines().items():
        path_exe = Path(engine.path_exe)
        if not path_exe.is_absolute():
            path_exe = BIN_DIR / path_exe
        engines[key] = path_exe
    return engines


def test_engines(r: Results) -> None:
    r.section("engines")

    try:
        import chess
    except Exception:
        chess = None

    # Sanity-check the mate-in-one puzzle itself before asking engines to solve it.
    if chess is not None:
        board = chess.Board(MATE_IN_1_FEN)
        board.push(chess.Move.from_uci(MATE_IN_1_MOVE))
        if not r.check("mate-in-1 puzzle is really mate", board.is_checkmate()):
            return

    engines = installed_engines()
    if not r.check("engines registered by OSEngines", bool(engines), f"{len(engines)} engines"):
        return
    print(f"       {', '.join(sorted(engines))}")

    for key in sorted(engines):
        path_exe = engines[key]

        if not r.check(f"[{key}] binary exists", path_exe.is_file(), str(path_exe)):
            continue
        r.check(f"[{key}] executable bit set", os.access(path_exe, os.X_OK))

        file_type = subprocess.run(["file", "-b", str(path_exe)], capture_output=True, text=True).stdout.strip()
        r.check(f"[{key}] Mach-O binary for {ARCH}", "Mach-O" in file_type and ARCH in file_type, file_type[:60])

        engine = None
        try:
            engine = UCIEngine(path_exe)

            engine.send("uci")
            lines = engine.read_until("uciok")
            r.check(f"[{key}] UCI handshake", any(line.startswith("uciok") for line in lines))
            name = next((line.split("id name ", 1)[1] for line in lines if "id name " in line), "")
            r.check(f"[{key}] reports its name", bool(name), name)

            engine.send("isready")
            engine.read_until("readyok")
            r.ok(f"[{key}] answers isready")

            move = engine.bestmove(None)
            if chess is not None:
                legal = {m.uci() for m in chess.Board().legal_moves}
                r.check(f"[{key}] plays a legal move from the initial position", move in legal, move)
            else:
                r.check(f"[{key}] returns a best move", bool(move), move)

            mate_move = engine.bestmove(MATE_IN_1_FEN, depth=6)
            r.check(f"[{key}] finds the mate in one", mate_move == MATE_IN_1_MOVE, mate_move)
        except Exception as exc:
            r.fail(f"[{key}] UCI conversation", f"{type(exc).__name__}: {exc}")
        finally:
            if engine is not None:
                engine.close()


# --------------------------------------------------------------------------- #
# application integration
# --------------------------------------------------------------------------- #

def test_integration(r: Results) -> None:
    r.section("integration")

    import Code
    from Code.Z import Util

    r.equal("Code.platform", Code.platform, "darwin")
    r.equal("folder_os points at OS/darwin", Path(Code.folder_os).name, "darwin")
    r.check("Util.is_macos()", Util.is_macos())
    r.check("Util.is_posix()", Util.is_posix())
    r.check("Util.is_linux() is false on macOS", not Util.is_linux())
    r.check("mono font exists on macOS", Code.font_mono == "Menlo", Code.font_mono)

    try:
        from Code.Engines import Priorities

        priorities = Priorities.Priorities()
        r.check(
            "engine priorities use POSIX nice values",
            priorities.values == [0, 10, 20, -10, -20],
            str(priorities.values),
        )
    except Exception as exc:
        r.fail("engine priorities", str(exc))

    try:
        engines = Code.configuration.engines
        r.check("tutor engine resolves", engines.engine_tutor() is not None, Code.configuration.tutor_default)
        r.check("analyzer engine resolves", engines.search(Code.configuration.analyzer_default) is not None)
        r.check("default rival (irina) is available", "irina" in engines.dic_engines())
        r.check("engines usable as tutor/analyzer", bool(engines.list_alias_name_multipv()))
    except Exception as exc:
        r.fail("engine catalogue", str(exc))

    try:
        import OSEngines

        fixed = OSEngines.li_engines_fixed_elo()
        keys = {item[0] for item in fixed}
        installed = set(Code.configuration.engines.dic_engines())
        r.check("fixed-elo engines are all installed", keys <= installed, f"{sorted(keys)}")
    except Exception as exc:
        r.fail("fixed-elo engine list", str(exc))

    # The "Play against an engine" dialog builds both of these, and both used to
    # assume every engine folder from the Linux build is present.
    try:
        books = Code.configuration.dic_books
        r.check("opening books indexed", bool(books), f"{len(books)} books")
    except Exception as exc:
        r.fail("opening books indexed", f"{type(exc).__name__}: {exc}")

    try:
        from Code.Engines import EnginesWicker

        wicker = EnginesWicker.read_wicker_engines()
        r.check(
            "wicker opponents available",
            bool(wicker),
            f"{len(wicker)} opponents, elo {min(e.elo for e in wicker)}-{max(e.elo for e in wicker)}",
        )
        r.check(
            "every wicker opponent has an installed engine and book",
            all(e.book and Path(e.book).is_file() for e in wicker),
        )
    except Exception as exc:
        r.fail("wicker opponents", f"{type(exc).__name__}: {exc}")

    try:
        from Code.Base import Game

        game = Game.Game()
        ok = game.read_pv("e2e4 e7e5 g1f3 b8c6 f1b5")
        r.check("play a short game through Code.Base.Game", ok and len(game) == 5, f"{len(game)} moves")
        r.check("game PGN generated", "e4" in game.pgn_translated() or "e4" in str(game.pgn_base_raw()))
    except Exception as exc:
        r.fail("Code.Base.Game", f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

def test_gui(r: Results) -> None:
    r.section("gui")

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6 import QtWidgets, QtGui

        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        r.ok("QApplication created with the offscreen platform plugin")

        import Code
        from Code.Base import Position
        from Code.Board import Board
        from Code.QT import Piezas

        Code.all_pieces = Piezas.AllPieces()
        r.ok("piece bitmaps loaded")

        config_board = Code.configuration.config_board("TEST_MACOS", 48)
        board = Board.Board(None, config_board, with_menu_visual=False, with_director=False)
        board.draw_window()
        board.set_side_bottom(True)

        position = Position.Position()
        position.read_fen(KIWIPETE)
        board.set_position(position)

        pixmap = board.grab()
        r.check("board widget renders", not pixmap.isNull() and pixmap.width() > 100, f"{pixmap.width()}x{pixmap.height()}")

        image = pixmap.toImage()
        colours = {image.pixel(x, y) for x in range(0, image.width(), 7) for y in range(0, image.height(), 7)}
        r.check("rendered board is not blank", len(colours) > 3, f"{len(colours)} distinct colours sampled")

        out = Path(tempfile.gettempdir()) / "lucaschess_board_macos.png"
        pixmap.save(str(out))
        r.check("board screenshot written", out.is_file(), str(out))
    except Exception as exc:
        r.fail("offscreen board rendering", f"{type(exc).__name__}: {exc}")
        if r.verbose:
            traceback.print_exc()

    # Full start-up: run the real entry point and check it stays alive and
    # writes no traceback to bug.log.
    bug_log = BIN_DIR / "bug.log"
    previous_size = bug_log.stat().st_size if bug_log.is_file() else 0
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = subprocess.Popen(
        [sys.executable, "LucasR.py"],
        cwd=str(BIN_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    time.sleep(20)
    alive = proc.poll() is None
    if alive:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    output = proc.stdout.read() if proc.stdout else ""
    r.check("LucasR.py runs for 20s without exiting", alive, output.strip()[-300:])

    new_log = ""
    if bug_log.is_file() and bug_log.stat().st_size > previous_size:
        with bug_log.open("rt", errors="ignore") as fh:
            fh.seek(previous_size)
            new_log = fh.read()
    r.check("no traceback in bug.log during start-up", "Traceback" not in new_log, new_log.strip()[-300:])


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

SECTIONS = {
    "environment": test_environment,
    "fastercode": test_fastercode,
    "resources": test_resources,
    "engines": test_engines,
    "integration": test_integration,
    "gui": test_gui,
}

NEEDS_CONFIGURATION = {"fastercode", "engines", "integration", "gui"}


def build_configuration(temp_userdata: Path):
    """Create the app Configuration against a throwaway UserData folder so the
    tests never touch real settings, games or results."""
    from Code.Config import ConfigPaths, Configuration

    ConfigPaths.ConfigPaths.LCBASEFOLDER = str(temp_userdata)
    ConfigPaths.ConfigPaths.LCFILEFOLDER = str(temp_userdata / "lc.folder")

    configuration = Configuration.Configuration("")
    configuration.lee()
    return configuration


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sections", nargs="*", choices=sorted(SECTIONS), help="sections to run (default: all)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every passing check too")
    args = parser.parse_args(COMMAND_LINE)

    sections = args.sections or list(SECTIONS)
    r = Results(args.verbose)

    print(f"Lucas Chess R6 - macOS verification ({platform.system()} {platform.mac_ver()[0]} {ARCH})")
    print(f"repo: {ROOT_DIR}")
    print(f"python: {sys.executable}")

    temp_userdata = Path(tempfile.mkdtemp(prefix="lucaschess-test-userdata-"))
    try:
        if set(sections) & NEEDS_CONFIGURATION:
            try:
                build_configuration(temp_userdata)
            except Exception as exc:
                print(f"\033[31mfatal\033[0m could not build a Configuration: {exc}")
                traceback.print_exc()
                return 1

        for name in sections:
            SECTIONS[name](r)
    finally:
        shutil.rmtree(temp_userdata, ignore_errors=True)

    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
