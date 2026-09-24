"""Barras de progresso e cancelamento para o tidal-dl-ultra."""

from __future__ import annotations

import signal
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from tidal_dl import ui

abort_event = threading.Event()
BAR_FORMAT_SEQ = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]"
BAR_FORMAT_UNKNOWN = "{desc}: {n_fmt}{unit} [{elapsed}]"


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GiB"


def _fmt_time(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


class SimpleBar:
    def __init__(self, total: int, desc: str, unit: str = "B", stream=None, clock=time.monotonic):
        self.total, self.desc, self.unit = int(total or 0), desc, unit
        self.n = 0
        self._stream = stream or sys.stdout
        self._clock = clock
        self._start = clock()
        self._last = 0.0
        self._postfix = ""
        self._drawn = False

    def _count(self, n: float) -> str:
        return _fmt_bytes(n) if self.unit == "B" else f"{int(n)}"

    def _line(self) -> str:
        elapsed = self._clock() - self._start
        columns = max(ui.progress_ncols(), 30)
        if self.total:
            percentage = min(self.n / self.total, 1.0)
            tail = f" {self._count(self.n)}/{self._count(self.total)}"
            rate = self.n / elapsed if elapsed > 0 else 0
            eta = (self.total - self.n) / rate if rate > 0 else 0
            tail += f" [{_fmt_time(elapsed)}<{_fmt_time(eta)}]"
            if self._postfix:
                tail += f" {self._postfix}"
            head = f"{self.desc}: {percentage * 100:3.0f}%|"
            room = max(columns - len(head) - len(tail) - 1, 4)
            filled = int(room * percentage)
            body = ui.block_char() * filled + " " * (room - filled)
            line = f"{head}{body}|{tail}"
        else:
            line = f"{self.desc}: {self._count(self.n)} [{_fmt_time(elapsed)}]"
            if self._postfix:
                line += f" {self._postfix}"
        return line[:columns]

    def _draw(self) -> None:
        now = self._clock()
        if now - self._last < 0.25:
            return
        self._last = now
        self._stream.write("\r" + self._line().ljust(min(ui.progress_ncols(), 120)))
        self._stream.flush()
        self._drawn = True

    def update(self, n: int = 1) -> None:
        self.n += n
        self._draw()

    def set_postfix_str(self, text: str) -> None:
        self._postfix = text
        self._draw()

    def close(self) -> None:
        if self._drawn:
            self._stream.write("\r" + " " * min(ui.progress_ncols(), 120) + "\r")
            self._stream.flush()
            self._drawn = False


class NullBar:
    n = 0

    def update(self, n: int = 1) -> None:
        self.n += n

    def set_postfix_str(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


def _load_tqdm():
    try:
        from tqdm import tqdm
        return tqdm
    except Exception:
        return None


@contextmanager
def track_bar(total: int, desc: str, *, unit: str = "B", enabled: bool = True,
              initial: int = 0, tqdm_cls=None) -> Iterator[object]:
    if not enabled or ui.is_quiet():
        bar = NullBar()
        try:
            yield bar
        finally:
            bar.close()
        return

    tqdm_type = tqdm_cls if tqdm_cls is not None else _load_tqdm()
    if tqdm_type is not None:
        bytes_mode = unit == "B"
        kwargs = {
            "total": total or None,
            "desc": desc,
            "initial": initial,
            "leave": False,
            "dynamic_ncols": True,
            "bar_format": BAR_FORMAT_SEQ if total else BAR_FORMAT_UNKNOWN,
        }
        if bytes_mode:
            kwargs.update(unit="iB", unit_scale=True, unit_divisor=1024)
        else:
            kwargs.update(unit=f" {unit}")
        bar = tqdm_type(**kwargs)
    else:
        bar = SimpleBar(total, desc, unit)
        if initial:
            bar.n = initial
    try:
        yield bar
    finally:
        bar.close()


@contextmanager
def sigint_guard() -> Iterator[None]:
    """Instala e restaura o handler de SIGINT, sinalizando o cancelamento.

    Alguns runtimes iOS/a-Shell não entregam ``signal.raise_signal`` ao
    handler Python da mesma forma que CPython desktop. O handler é instalado
    normalmente; o polling de ``abort_event`` continua sendo o mecanismo
    principal usado pelos downloads.
    """
    abort_event.clear()
    try:
        original = signal.getsignal(signal.SIGINT)
    except (ValueError, OSError, AttributeError):
        original = signal.SIG_DFL

    def handler(signum, frame):
        abort_event.set()
        raise KeyboardInterrupt

    installed = False
    try:
        signal.signal(signal.SIGINT, handler)
        installed = True
    except (ValueError, OSError, AttributeError):
        pass

    try:
        yield
    finally:
        if installed:
            try:
                signal.signal(signal.SIGINT, original)
            except (ValueError, OSError, TypeError):
                pass
