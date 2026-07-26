"""Minimal UCI driver, enough to prove an engine binary actually works.

Shared by run_tests.py (engines in the source tree) and test_app_bundle.py
(engines inside an installed .app).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# 6k1/5ppp/8/8/8/8/8/R5K1 w: Ra8 is mate, the king's escape squares are its own
# pawns and the rest of the eighth rank.
MATE_IN_1_FEN = "6k1/5ppp/8/8/8/8/8/R5K1 w - - 0 1"
MATE_IN_1_MOVE = "a1a8"

INITIAL_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class UCIEngine:
    def __init__(self, path_exe: str | Path, timeout: float = 25.0):
        self.path_exe = Path(path_exe)
        self.timeout = timeout
        env = dict(os.environ)
        # Same as EngineRun does, for engines that load a dylib next to them.
        env["DYLD_LIBRARY_PATH"] = f"{self.path_exe.parent}:{env.get('DYLD_LIBRARY_PATH', '')}"
        self.process = subprocess.Popen(
            [str(self.path_exe)],
            cwd=str(self.path_exe.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=env,
        )

    def send(self, command: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def read_until(self, token: str) -> list[str]:
        """Collect output lines until one starts with `token`. Raises on timeout."""
        deadline = time.time() + self.timeout
        lines: list[str] = []
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"engine exited with code {self.process.returncode}")
            line = self.process.stdout.readline()  # type: ignore[union-attr]
            if not line:
                raise RuntimeError("engine closed its output")
            line = line.strip()
            if line:
                lines.append(line)
            if line.startswith(token):
                return lines
        raise TimeoutError(f"no {token!r} within {self.timeout:.0f}s")

    def handshake(self) -> str:
        """Return the engine's reported name."""
        self.send("uci")
        lines = self.read_until("uciok")
        # GreKo prefixes its first line with a banner, so do not anchor on the start.
        return next((line.split("id name ", 1)[1] for line in lines if "id name " in line), "")

    def isready(self) -> None:
        self.send("isready")
        self.read_until("readyok")

    def bestmove(self, fen: str | None, depth: int = 8, movetime_ms: int = 3000) -> str:
        self.send("ucinewgame")
        self.send("position " + (f"fen {fen}" if fen else "startpos"))
        self.isready()
        self.send(f"go depth {depth}")
        try:
            lines = self.read_until("bestmove")
        except TimeoutError:
            # A few old engines ignore "go depth"; fall back to a timed search.
            self.send("stop")
            self.send(f"go movetime {movetime_ms}")
            lines = self.read_until("bestmove")
        return lines[-1].split()[1]

    def close(self) -> None:
        try:
            self.send("quit")
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()
        finally:
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    try:
                        stream.close()
                    except Exception:
                        pass

    def __enter__(self) -> "UCIEngine":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
