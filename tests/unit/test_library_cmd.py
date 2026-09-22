"""Testes da camada de terminal (scan / library / sync-favorites)."""

import asyncio
from types import SimpleNamespace

import pytest

from tidal_dl import library_cmd as lc
from tidal_dl import library_scan as ls
from tidal_dl import sentinel as sn
from tidal_dl.library_db import LibraryDB
from tidal_dl.models import Page


@pytest.fixture(autouse=True)
def sem_mutagen(monkeypatch):
    monkeypatch.setattr(ls, "_read_tags", lambda p: {})


@pytest.fixture
def lib(tmp_path):
    return LibraryDB(tmp_path / "l.db")


def _args(**kw):
    base = dict(DIR=None, dry_run=False, no_sentinel=False, no_adopt=False, no_review=True,
                rescan=False, max_depth=4, fuzzy_threshold=0.85, json=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _pasta(root, rel, n=1):
    p = root / rel
    p.mkdir(parents=True)
    for i in range(n):
        (p / f"{i}.flac").write_bytes(b"x")
    return p


def test_scan_marca_e_escreve_json(tmp_path, lib):
    lib.upsert_album("tidal", "1", "B", "A")
    _pasta(tmp_path / "m", "A - B")
    _pasta(tmp_path / "m", "Sem - Match")
    out = tmp_path / "r.json"
    rc = asyncio.run(lc.cmd_scan(_args(DIR=str(tmp_path / "m"), json=str(out)), directory="x", lib=lib))
    assert rc == 0 and out.exists()
    assert lib.get_album_by_source_id("tidal", "1")["download_status"] == "complete"


def test_scan_registra_no_banco_de_dedup(tmp_path, lib):
    from tidal_dl import db
    lib.upsert_album("tidal", "1", "B", "A")
    pasta = _pasta(tmp_path / "m", "A - B")
    dbp = str(tmp_path / "t.db")
    asyncio.run(lc.cmd_scan(_args(DIR=str(tmp_path / "m")), directory="x", quality=3, downloads_db=dbp, lib=lib))
    rec = db.get_record(dbp, 1, "album")
    assert rec["saved_path"] == str(pasta) and rec["quality"] == 3


def test_scan_pasta_inexistente(tmp_path, lib):
    assert asyncio.run(lc.cmd_scan(_args(DIR=str(tmp_path / "nada")), directory="x", lib=lib)) == 1


def test_library_acoes(tmp_path, lib, capsys):
    a = lib.upsert_album("tidal", "1", "B", "A")
    lib.update_status(a, "downloading")
    ns = lambda **kw: SimpleNamespace(TARGET=None, limit=None, fix=False, dry_run=False, **kw)
    for action in ("status", "missing", "history", "reset-stuck"):
        assert asyncio.run(lc.cmd_library(ns(action=action), directory=str(tmp_path), lib=lib)) == 0
    assert lib.get_album(a)["download_status"] == "not_downloaded"


def test_library_unmark_e_reconcile(tmp_path, lib):
    a = lib.upsert_album("tidal", "1", "B", "A")
    pasta = _pasta(tmp_path, "A - B")
    ls.mark_album_downloaded(lib, a, folder=pasta)
    ns = lambda **kw: SimpleNamespace(limit=None, fix=False, dry_run=False, **kw)
    assert asyncio.run(lc.cmd_library(ns(action="unmark", TARGET="1"), directory="x", lib=lib)) == 0
    assert not sn.has_sentinel(pasta)
    assert asyncio.run(lc.cmd_library(ns(action="unmark", TARGET="999"), directory="x", lib=lib)) == 1
    assert asyncio.run(lc.cmd_library(ns(action="reconcile", TARGET=str(tmp_path)), directory="x", lib=lib)) == 0


class FakeTidal:
    def __init__(self, ok=True):
        self.downloads_db = None
        self.settings = SimpleNamespace(write_sentinel=True)
        self.baixados, self.ok = [], ok
        entries = [{"created": "x", "item": {"id": 1, "title": "T", "artist": {"name": "A"}}}]

        class API:
            async def favorite_albums_page(self, *, limit=100, offset=0):
                return Page(entries[offset:offset + limit], len(entries), limit, offset)

        self.api = API()

    async def download_from_id(self, album_id, kind="album", **kw):
        assert kind == "album"
        self.baixados.append(album_id)
        return self.ok


def _sf(**kw):
    base = dict(download_new=False, download_missing=False, dry_run=False, yes=True, every=None, limit=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_sync_favorites_download(lib):
    t = FakeTidal()
    assert asyncio.run(lc.cmd_sync_favorites(_sf(download_new=True), t, lib=lib)) == 0 and t.baixados == ["1"]


def test_sync_favorites_falha_retorna_1_e_dry_run(lib):
    assert asyncio.run(lc.cmd_sync_favorites(_sf(download_new=True), FakeTidal(ok=False), lib=lib)) == 1
    lib2 = LibraryDB(lib.path + ".2")
    t = FakeTidal()
    assert asyncio.run(lc.cmd_sync_favorites(_sf(dry_run=True, download_new=True), t, lib=lib2)) == 0
    assert t.baixados == [] and lib2.get_albums() == []


def test_sync_favorites_every(lib):
    esperas = []

    async def sleep(s):
        esperas.append(s)
        if len(esperas) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(lc.cmd_sync_favorites(_sf(every=5), FakeTidal(), lib=lib, sleep=sleep))
    assert esperas == [300, 300] and len(lib.get_sync_history("tidal")) == 2
