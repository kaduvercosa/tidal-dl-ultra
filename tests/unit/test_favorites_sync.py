"""Testes do sync de favoritos do Tidal (paginação segura, diff, download)."""

import asyncio

import pytest

from tidal_dl import favorites_sync as fs
from tidal_dl.library_db import LibraryDB
from tidal_dl.models import Page


class FakeAPI:
    def __init__(self, albums, total=None, fail_on_call=None):
        self.albums, self.total, self.fail_on_call, self.calls = albums, total, fail_on_call, 0

    async def favorite_albums_page(self, *, limit=100, offset=0):
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("rede caiu")
        total = len(self.albums) if self.total is None else self.total
        return Page(self.albums[offset:offset + limit], total, limit, offset)


def _entry(i, **kw):
    item = {"id": i, "title": f"T{i}", "artist": {"name": "A"}, "numberOfTracks": 10, "releaseDate": "2020-01-02",
            "cover": "aa-bb", "upc": f"u{i}"}
    item.update(kw)
    return {"created": "2024-01-01T00:00:00", "item": item}


@pytest.fixture
def lib(tmp_path):
    return LibraryDB(tmp_path / "l.db")


def test_extract_desembrulha_e_omite_qualidade():
    a = fs.extract_album_data(_entry(1, version="Deluxe"))
    assert a["title"] == "T1 (Deluxe)" and a["track_count"] == 10 and a["upc"] == "u1"
    assert "bit_depth" not in a and a["cover_url"].endswith("/aa/bb/640x640.jpg")
    assert a["added_to_library_at"] == "2024-01-01T00:00:00"
    assert fs.extract_album_data({"item": {"title": "sem id"}}) is None
    assert fs.extract_album_data({"id": 5, "title": "plano"})["source_album_id"] == "5"


def test_paginacao_completa():
    api = FakeAPI([_entry(i) for i in range(5)])
    items, total = asyncio.run(fs.fetch_all_favorite_albums(api, page_size=2))
    assert len(items) == 5 and total == 5 and api.calls == 3


def test_lista_vazia_e_completa():
    items, total = asyncio.run(fs.fetch_all_favorite_albums(FakeAPI([])))
    assert items == [] and total == 0


def test_erro_de_rede_propaga():
    with pytest.raises(fs.FavoritesFetchError):
        asyncio.run(fs.fetch_all_favorite_albums(FakeAPI([_entry(i) for i in range(4)], fail_on_call=2), page_size=2))


def test_diff_novos_e_removidos(lib):
    lib.upsert_album("tidal", "old", "Velho", "A")
    r = asyncio.run(fs.refresh_library(lib, FakeAPI([_entry(1), _entry(2)])))
    assert r["new"] == 2 and r["removed"] == 1 and r["complete"]
    assert lib.get_album_by_source_id("tidal", "old")["removed_from_service"] == 1
    assert lib.get_album_by_source_id("tidal", "1")["added_to_library_at"] == "2024-01-01T00:00:00"
    r2 = asyncio.run(fs.refresh_library(lib, FakeAPI([_entry(1), _entry(2)])))
    assert r2["new"] == 0 and r2["removed"] == 0


def test_paginacao_incompleta_nao_marca_remocao(lib):
    lib.upsert_album("tidal", "old", "Velho", "A")
    r = asyncio.run(fs.refresh_library(lib, FakeAPI([_entry(1)], total=50)))
    assert not r["complete"] and r["removed"] == 0
    assert lib.get_album_by_source_id("tidal", "old")["removed_from_service"] == 0


def test_dry_run_nao_grava(lib):
    r = asyncio.run(fs.refresh_library(lib, FakeAPI([_entry(1)]), dry_run=True))
    assert r["new"] == 1 and lib.get_albums() == []


def test_run_sync_baixa_novos_e_historico(tmp_path, lib):
    baixados = []

    async def dl(album_id):
        baixados.append(album_id)
        return album_id == "1"

    res = asyncio.run(fs.run_sync(lib, FakeAPI([_entry(1), _entry(2)]), download_new=True, download_fn=dl))
    assert sorted(baixados) == ["1", "2"] and res["download"]["downloaded"] == 1 and res["download"]["failed"] == 1
    assert lib.get_album_by_source_id("tidal", "1")["download_status"] == "complete"
    assert lib.get_album_by_source_id("tidal", "2")["download_status"] == "failed"
    assert lib.get_sync_history("tidal")[0]["albums_new"] == 2


def test_usa_caminho_do_banco_de_dedup(tmp_path, lib):
    from tidal_dl import db, sentinel
    pasta = tmp_path / "Disco"
    pasta.mkdir()
    dbp = str(tmp_path / "t.db")
    db.mark_downloaded(dbp, 1, "album", quality=4, saved_path=str(pasta))

    async def dl(_):
        return True

    asyncio.run(fs.run_sync(lib, FakeAPI([_entry(1)]), download_new=True, download_fn=dl, downloads_db=dbp))
    row = lib.get_album_by_source_id("tidal", "1")
    assert row["local_folder_path"] == str(pasta) and sentinel.has_sentinel(pasta)


def test_falha_de_rede_marca_run_failed(lib):
    with pytest.raises(fs.FavoritesFetchError):
        asyncio.run(fs.run_sync(lib, FakeAPI([_entry(1)], fail_on_call=1)))
    assert lib.get_sync_history("tidal")[0]["status"] == "failed"


def test_confirmacao_negada_e_limit(lib):
    async def dl(_):
        raise AssertionError("não devia baixar")

    async def no(_):
        return False

    assert asyncio.run(fs.run_sync(lib, FakeAPI([_entry(1)]), download_new=True, download_fn=dl, confirm=no))["targets"] == 0
    for i in range(3):
        lib.upsert_album("tidal", f"x{i}", f"T{i}", "A")
    assert len(fs.pick_download_targets(lib, missing=True, limit=2)) == 2
