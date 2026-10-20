"""SSH-туннель к локальному API на VPS; перезапускается, если процесс умер."""
from __future__ import annotations

import subprocess


class Tunnel:
    def __init__(self, args: list[str]):
        self.args = args
        self.proc: subprocess.Popen | None = None

    def ensure(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(self.args, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
