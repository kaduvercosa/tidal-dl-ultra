"""Barras de progresso do tidal-dl-ultra (tqdm quando existe, barra própria se não).

MODOS (mesma lógica do qobuz-dl-ultra)
--------------------------------------
* **Sequencial** (``max_workers = 1`` ou ``--delay`` ativo): uma faixa por vez,
  com barra de progresso em tempo real::

      [~] Em Progresso: 03. Song
        ⬇️:  45%|██████████       | 12.3MiB/27.0MiB [00:05<00:06]

* **Paralelo** (``max_workers > 1``): várias faixas ao mesmo tempo. Barras
  desenhadas por cima umas das outras ficam ilegíveis, então (igual ao
  qobuz-dl-ultra) cada faixa mostra linhas ``Em Progresso`` / ``Concluído``.

O tqdm é usado quando instalado (é Python puro, instala no a-Shell). Sem ele a
``SimpleBar`` entrega o mesmo visual com ``\\r``. ``ui.emit`` já escreve via
``tqdm.write`` para as mensagens não cortarem a barra no meio.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from tidal_dl import ui

# Sinal global de cancelamento (CTRL+C): o loop de download confere entre chunks.
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
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class SimpleBar:
    """Plano B sem tqdm: uma linha reescrita com ``\\r`` (no máx. ~4x por segundo)."""

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
        cols = max(ui.progress_ncols(), 30)
        if self.total:
            pct = min(self.n / self.total, 1.0)
            tail = f" {self._count(self.n)}/{self._count(self.total)}"
            rate = self.n / elapsed if elapsed > 0 else 0
            eta = (self.total - self.n) / rate if rate > 0 else 0
            tail += f" [{_fmt_time(elapsed)}<{_fmt_time(eta)}]"
            if self._postfix:
                tail += f" {self._postfix}"  # entra no orçamento da linha (senão o corte o esconde)
            head = f"{self.desc}: {pct * 100:3.0f}%|"
            room = max(cols - len(head) - len(tail) - 1, 4)
            filled = int(room * pct)
            body = ui.block_char() * filled + " " * (room - filled)
            line = f"{head}{body}|{tail}"
        else:
            line = f"{self.desc}: {self._count(self.n)} [{_fmt_time(elapsed)}]"
            if self._postfix:
                line += f" {self._postfix}"
        return line[:cols]

    def _draw(self, force: bool = False) -> None:
        now = self._clock()
        if not force and now - self._last < 0.25:
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
        if self._drawn:  # apaga a linha (equivale a leave=False do tqdm)
            self._stream.write("\r" + " " * min(ui.progress_ncols(), 120) + "\r")
            self._stream.flush()
            self._drawn = False


class NullBar:
    """Barra desligada (modo paralelo, --quiet, --no-progress): não faz nada."""

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
def track_bar(
    total: int,
    desc: str,
    *,
    unit: str = "B",
    enabled: bool = True,
    initial: int = 0,
    tqdm_cls=None,
) -> Iterator[object]:
    """Barra de UMA faixa. ``unit="B"`` = bytes; ``"seg"`` = segmentos DASH.

    Desligada (``NullBar``) quando ``enabled`` é False ou ``--quiet``.
    """
    if not enabled or ui.is_quiet():
        bar: object = NullBar()
        try:
            yield bar
        finally:
            bar.close()
        return

    tq = tqdm_cls if tqdm_cls is not None else _load_tqdm()
    if tq is not None:
        bytes_mode = unit == "B"
        kwargs = dict(
            total=total or None,
            desc=desc,
            initial=initial,
            leave=False,
            dynamic_ncols=True,
            bar_format=BAR_FORMAT_SEQ if total else BAR_FORMAT_UNKNOWN,
        )
        if bytes_mode:
            kwargs.update(unit="iB", unit_scale=True, unit_divisor=1024)
        else:
            kwargs.update(unit=f" {unit}")
        bar = tq(**kwargs)
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
    """CTRL+C durante o download: sinaliza ``abort_event`` e levanta KeyboardInterrupt.

    Igual ao qobuz-dl-ultra. O handler original é restaurado ao sair. Onde não
    dá para instalar handler (thread secundária, ambiente restrito), segue sem.
    """
    abort_event.clear()
    original = None
    installed = False
    try:
        original = signal.getsignal(signal.SIGINT)

        def handler(signum, frame):
            abort_event.set()
            raise KeyboardInterrupt

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
