"""Testes do catálogo local (library.db)."""

import pytest

from tidal_dl.library_db import (
    STATUS_COMPLETE, STATUS_DOWNLOADING, STATUS_INCOMPLETE, STATUS_NOT_DOWNLOADED,
    AlbumNotFoundError, LibraryDB,
)


@pytest.fixture
def lib(tmp_path):
    return LibraryDB(tmp_path / "sub" / "library.db")


def test_upsert_nao_apaga_campos_com_none(lib):
    aid = lib.upsert_album("tidal", "1", "T", "A", cover_url="http://c", bit_depth=24)
    lib.upsert_album("tidal", "1", "T2", "A", cover_url=None, bit_depth=None)
    row = lib.get_album(aid)
    assert row["title"] == "T2"
    assert row["cover_url"] == "http://c" and row["bit_depth"] == 24


def test_upsert_preserva_estado_de_download(lib):
    aid = lib.upsert_album("tidal", "1", "T", "A")
    lib.set_download_state(aid, downloaded=True, local_folder_path="/x")
    lib.upsert_album("tidal", "1", "T", "A", track_count=5)
    row = lib.get_album(aid)
    assert row["download_status"] == STATUS_COMPLETE and row["local_folder_path"] == "/x"


def test_set_download_state_preserva_caminho_ao_marcar_e_limpa_ao_desmarcar(lib):
    aid = lib.upsert_album("tidal", "1", "T", "A")
    lib.set_download_state(aid, downloaded=True, local_folder_path="/x")
    lib.set_download_state(aid, downloaded=True)
    assert lib.get_album(aid)["local_folder_path"] == "/x"
    old = lib.set_download_state(aid, downloaded=False)
    assert old["local_folder_path"] == "/x"
    row = lib.get_album(aid)
    assert row["local_folder_path"] is None and row["download_status"] == STATUS_NOT_DOWNLOADED


def test_album_inexistente(lib):
    with pytest.raises(AlbumNotFoundError):
        lib.set_download_state(99, downloaded=True)


def test_mark_removed_e_retorno(lib):
    lib.upsert_album("tidal", "1", "T1", "A")
    lib.upsert_album("tidal", "2", "T2", "A")
    removed = lib.mark_removed("tidal", ["1"])
    assert [r["source_album_id"] for r in removed] == ["2"]
    assert lib.mark_removed("tidal", ["1"]) == []  # idempotente
    lib.upsert_album("tidal", "2", "T2", "A")  # voltou
    assert lib.get_album_by_source_id("tidal", "2")["removed_from_service"] == 0


def test_reset_stuck_e_relatorios(lib):
    a = lib.upsert_album("tidal", "1", "T1", "A")
    b = lib.upsert_album("tidal", "2", "T2", "A")
    lib.update_status(a, STATUS_DOWNLOADING)
    lib.update_status(b, STATUS_INCOMPLETE)
    assert len(lib.stuck_albums()) == 1
    assert lib.reset_stuck() == 1
    assert lib.get_album(a)["download_status"] == STATUS_NOT_DOWNLOADED
    assert lib.get_album(b)["download_status"] == STATUS_INCOMPLETE
    counts = {(r["download_status"]): r["cnt"] for r in lib.status_counts()}
    assert counts == {STATUS_NOT_DOWNLOADED: 1, STATUS_INCOMPLETE: 1}


def test_historico_de_sync(lib):
    r1 = lib.create_sync_run("tidal")
    lib.complete_sync_run(r1, albums_found=3, albums_new=1)
    r2 = lib.create_sync_run("tidal")
    lib.fail_sync_run(r2)
    r3 = lib.create_sync_run("tidal")
    lib.interrupt_sync_run(r3)
    hist = lib.get_sync_history("tidal")
    assert [h["status"] for h in hist] == ["interrupted", "failed", "complete"]
    assert hist[2]["albums_found"] == 3


def test_schema_reabre_sem_erro(tmp_path):
    LibraryDB(tmp_path / "l.db").upsert_album("tidal", "1", "T", "A")
    assert len(LibraryDB(tmp_path / "l.db").get_albums()) == 1
