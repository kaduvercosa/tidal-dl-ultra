"""Testes das barras de progresso e do guard de CTRL+C."""

import io
import os
import signal

import pytest

from tidal_dl import progress, ui


class FakeTqdm:
    instances = []

    def __init__(self, **kw):
        self.kw, self.n, self.closed, self.postfix = kw, kw.get("initial", 0), False, ""
        FakeTqdm.instances.append(self)

    def update(self, n=1):
        self.n += n

    def set_postfix_str(self, s):
        self.postfix = s

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def limpo():
    FakeTqdm.instances = []
    ui.configure(quiet=False)
    progress.abort_event.clear()


def test_tqdm_em_bytes():
    with progress.track_bar(1000, "  ⬇️", tqdm_cls=FakeTqdm, initial=100) as bar:
        bar.update(50)
    kw = FakeTqdm.instances[0].kw
    assert kw["total"] == 1000 and kw["unit"] == "iB" and kw["unit_scale"] and kw["unit_divisor"] == 1024
    assert kw["leave"] is False and kw["dynamic_ncols"] and kw["initial"] == 100
    assert "{percentage" in kw["bar_format"] and "{remaining}" in kw["bar_format"]
    assert FakeTqdm.instances[0].n == 150 and FakeTqdm.instances[0].closed


def test_tqdm_em_segmentos_e_tamanho_desconhecido():
    with progress.track_bar(40, "  ↪️", unit="seg", tqdm_cls=FakeTqdm) as bar:
        bar.set_postfix_str("1.2MB")
    kw = FakeTqdm.instances[0].kw
    assert kw["unit"] == " seg" and "unit_scale" not in kw and FakeTqdm.instances[0].postfix == "1.2MB"
    with progress.track_bar(0, "x", tqdm_cls=FakeTqdm):
        pass
    assert FakeTqdm.instances[1].kw["total"] is None and "percentage" not in FakeTqdm.instances[1].kw["bar_format"]


def test_desligada_por_flag_e_por_quiet():
    with progress.track_bar(10, "x", enabled=False, tqdm_cls=FakeTqdm) as b:
        b.update(1)
    ui.configure(quiet=True)
    try:
        with progress.track_bar(10, "x", tqdm_cls=FakeTqdm):
            pass
    finally:
        ui.configure(quiet=False)
    assert FakeTqdm.instances == [] and isinstance(b, progress.NullBar)


def test_simplebar_desenha_e_apaga():
    buf, t = io.StringIO(), [0.0]
    bar = progress.SimpleBar(2_000_000, "  ⬇️", stream=buf, clock=lambda: t[0])
    for _ in range(5):
        t[0] += 1
        bar.update(400_000)
    last = buf.getvalue().split("\r")[-1]
    assert "100%" in last and "MiB/" in last and "[00:05<00:00]" in last
    bar.close()
    assert buf.getvalue().endswith("\r") and "100%" not in buf.getvalue().split("\r")[-2]


def test_simplebar_sem_total_e_segmentos():
    buf, t = io.StringIO(), [0.0]
    bar = progress.SimpleBar(0, "x", stream=buf, clock=lambda: t[0])
    t[0] = 2
    bar.update(2048)
    assert "2.0KiB" in buf.getvalue()
    seg, t2 = io.StringIO(), [5.0]
    b2 = progress.SimpleBar(10, "y", unit="seg", stream=seg, clock=lambda: t2[0])
    b2.update(3)
    t2[0] = 6.0
    b2.set_postfix_str("1.0MiB")
    assert "3/10" in seg.getvalue() and "1.0MiB" in seg.getvalue()


def test_simplebar_limita_taxa_de_redesenho():
    buf, t = io.StringIO(), [0.0]
    bar = progress.SimpleBar(100, "x", stream=buf, clock=lambda: t[0])
    t[0] = 1.0
    for _ in range(50):
        bar.update(1)  # mesmo instante: só o primeiro desenha
    assert buf.getvalue().count("\r") == 1


def test_formatadores():
    assert progress._fmt_bytes(512) == "512B" and progress._fmt_bytes(1536) == "1.5KiB"
    assert progress._fmt_time(65) == "01:05" and progress._fmt_time(3725) == "1:02:05" and progress._fmt_time(-3) == "00:00"


def test_sigint_guard_sinaliza_e_restaura():
    if not hasattr(signal, "raise_signal"):
        return
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        with progress.sigint_guard():
            signal.raise_signal(signal.SIGINT)
    assert progress.abort_event.is_set() and signal.getsignal(signal.SIGINT) == before


def test_sigint_guard_limpa_evento_ao_entrar():
    progress.abort_event.set()
    with progress.sigint_guard():
        assert not progress.abort_event.is_set()
