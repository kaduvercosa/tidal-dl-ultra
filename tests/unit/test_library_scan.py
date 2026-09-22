"""Testes do scan da biblioteca (matching, multi-disco, estados, reconcile)."""

import asyncio
from pathlib import Path

import pytest

from tidal_dl import library_scan as ls
from tidal_dl import sentinel as sn
from tidal_dl.library_db import LibraryDB


@pytest.fixture(autouse=True)
def sem_mutagen(monkeypatch):
    """Por padrão nenhuma tag: o match cai no nome da pasta."""
    monkeypatch.setattr(ls, "_read_tags", lambda p: {})


@pytest.fixture
def lib(tmp_path):
    return LibraryDB(tmp_path / "lib.db")


def _mk(root: Path, rel: str, n: int) -> Path:
    p = root / rel
    p.mkdir(parents=True)
    for i in range(n):
        (p / f"{i + 1:02}.flac").write_bytes(b"x")
    return p


def test_normalize():
    assert ls.normalize("The Wall (Remastered) [FLAC 24]") == "wall"
    assert ls.normalize("[INCOMPLETE] Café") == "cafe"
    assert ls.normalize(None) == ""


def test_folder_state_e_disc():
    assert ls.folder_state("[IN PROGRESS] X") == "in_progress"
    assert ls.folder_state("[INCOMPLETE] X") == "incomplete"
    assert ls.folder_state("X") == "ok"
    assert ls.is_disc_folder("CD 01") and ls.is_disc_folder("Disco 2")
    assert not ls.is_disc_folder("Discovery")


def test_find_album_folders_multidisco_e_ocultas(tmp_path):
    _mk(tmp_path, "Album/Um - Simples", 1)
    _mk(tmp_path, "Album/Dois - Duplo/CD 01", 1)
    _mk(tmp_path, "Album/Dois - Duplo/CD 02", 1)
    _mk(tmp_path, ".oculta/X - Y", 1)
    folders, _ = ls.find_album_folders(tmp_path)
    nomes = sorted(f.name for f in folders)
    assert nomes == ["Dois - Duplo", "Um - Simples"]


def _run(lib, root, **kw):
    return asyncio.run(ls.run_scan(lib, root, **kw))


def test_match_exato_marca_e_grava_sentinela(tmp_path, lib):
    aid = lib.upsert_album("tidal", "111", "Discovery", "Daft Punk", track_count=2, bit_depth=24)
    pasta = _mk(tmp_path / "m", "Album/Daft Punk - Discovery (2001) [FLAC 24]", 2)
    rep = _run(lib, tmp_path / "m")
    assert len(rep["auto_matched"]) == 1
    row = lib.get_album(aid)
    assert row["download_status"] == "complete" and row["local_folder_path"] == str(pasta)
    assert sn.read_sentinel(pasta)["album_id"] == "111"
    # segunda passada: já tem sentinela
    assert _run(lib, tmp_path / "m")["sentinel_skipped"] == 1


def test_dry_run_nao_grava(tmp_path, lib):
    aid = lib.upsert_album("tidal", "1", "Discovery", "Daft Punk")
    pasta = _mk(tmp_path / "m", "Daft Punk - Discovery", 1)
    rep = _run(lib, tmp_path / "m", apply=False)
    assert len(rep["auto_matched"]) == 1 and rep["dry_run"]
    assert lib.get_album(aid)["download_status"] == "not_downloaded"
    assert not sn.has_sentinel(pasta)


def test_contagem_divergente_vai_para_revisao(tmp_path, lib):
    lib.upsert_album("tidal", "1", "Discovery", "Daft Punk", track_count=14)
    _mk(tmp_path / "m", "Daft Punk - Discovery", 3)
    rep = _run(lib, tmp_path / "m")
    assert not rep["auto_matched"] and len(rep["review"]) == 1
    assert "track_count_mismatch" in rep["review"][0]["candidates"][0]["reason"]


def test_bit_depth_divergente_vai_para_revisao(tmp_path, lib, monkeypatch):
    lib.upsert_album("tidal", "1", "Discovery", "Daft Punk", bit_depth=24)
    monkeypatch.setattr(ls, "_read_tags", lambda p: {"artist": "Daft Punk", "album": "Discovery", "bit_depth": 16})
    _mk(tmp_path / "m", "x/y", 1)
    rep = _run(lib, tmp_path / "m")
    assert len(rep["review"]) == 1 and not rep["auto_matched"]


def test_dois_candidatos_nunca_auto(tmp_path, lib):
    lib.upsert_album("tidal", "1", "Greatest Hits", "Queen")
    lib.upsert_album("tidal", "2", "Greatest Hits (Deluxe)", "Queen")
    _mk(tmp_path / "m", "Queen - Greatest Hits", 1)
    rep = _run(lib, tmp_path / "m")
    assert not rep["auto_matched"] and len(rep["review"]) == 1
    assert len(rep["review"][0]["candidates"]) == 2


def test_fuzzy_sugere_mas_nunca_marca(tmp_path, lib):
    lib.upsert_album("tidal", "1", "The Wall", "Pink Floyd")
    _mk(tmp_path / "m", "Pink Floyd - The Wal", 1)
    rep = _run(lib, tmp_path / "m")
    assert not rep["auto_matched"]
    assert rep["review"][0]["candidates"][0]["reason"].startswith("fuzzy")


def test_sem_match(tmp_path, lib):
    _mk(tmp_path / "m", "Ninguem - Nada", 1)
    assert len(_run(lib, tmp_path / "m")["unmatched"]) == 1


def test_match_por_tag_de_id(tmp_path, lib, monkeypatch):
    aid = lib.upsert_album("tidal", "abc", "Nome Diferente", "Outro", track_count=1)
    monkeypatch.setattr(ls, "_read_tags", lambda p: {"artist": "X", "album": "Y", "service_album_id": "abc"})
    _mk(tmp_path / "m", "q/w", 1)
    rep = _run(lib, tmp_path / "m")
    assert rep["auto_matched"][0]["reason"] == "tag_id"
    assert lib.get_album(aid)["download_status"] == "complete"


def test_adocao_de_album_fora_dos_favoritos(tmp_path, lib, monkeypatch):
    monkeypatch.setattr(ls, "_read_tags", lambda p: {"artist": "X", "album": "Y", "service_album_id": "zzz"})
    pasta = _mk(tmp_path / "m", "q/w", 2)
    rep = _run(lib, tmp_path / "m")
    assert len(rep["adopted"]) == 1
    row = lib.get_album_by_source_id("tidal", "zzz")
    assert row["download_status"] == "complete" and row["track_count"] == 2
    assert sn.has_sentinel(pasta)
    # --no-adopt
    lib2 = LibraryDB(tmp_path / "l2.db")
    rep2 = _run(lib2, tmp_path / "m", rescan=True, adopt_unknown=False)
    assert rep2["unmatched"] and lib2.get_album_by_source_id("tidal", "zzz") is None


def test_pasta_incompleta_nao_vira_completa(tmp_path, lib, monkeypatch):
    aid = lib.upsert_album("tidal", "abc", "Y", "X", track_count=5)
    monkeypatch.setattr(ls, "_read_tags", lambda p: {"artist": "X", "album": "Y", "service_album_id": "abc"})
    pasta = _mk(tmp_path / "m", "[INCOMPLETE] X - Y", 2)
    rep = _run(lib, tmp_path / "m")
    assert len(rep["incomplete"]) == 1 and not rep["auto_matched"]
    assert lib.get_album(aid)["download_status"] == "incomplete"
    assert not sn.has_sentinel(pasta)


def test_dedup_writer_e_falha_isolada(tmp_path, lib):
    lib.upsert_album("tidal", "1", "A", "Art")
    lib.upsert_album("tidal", "2", "B", "Art")
    _mk(tmp_path / "m", "Art - A", 1)
    _mk(tmp_path / "m", "Art - B", 1)
    chamados = []

    async def writer(album, folder, meta):
        if album["source_album_id"] == "1":
            raise RuntimeError("boom")
        chamados.append(album["source_album_id"])

    rep = _run(lib, tmp_path / "m", dedup_writer=writer)
    assert chamados == ["2"] and len(rep["failed"]) == 1


def test_raiz_inexistente(tmp_path, lib):
    assert _run(lib, tmp_path / "nada")["status"] == "root_not_found"


def test_interrupcao_por_stop_event(tmp_path, lib):
    _mk(tmp_path / "m", "A - B", 1)
    ev = asyncio.Event()
    ev.set()
    assert _run(lib, tmp_path / "m", stop_event=ev)["status"] == "interrupted"


def test_reconcile_sentinelas(tmp_path, lib):
    root = tmp_path / "m"
    ok = _mk(root, "a", 2)
    sn.write_sentinel(ok, sn.build_payload("tidal", "10", "T", "A", 2))
    parcial = _mk(root, "b", 1)
    sn.write_sentinel(parcial, sn.build_payload("tidal", "11", "T", "A", 3))
    sumido = lib.upsert_album("tidal", "99", "Sumiu", "A")
    lib.set_download_state(sumido, downloaded=True, local_folder_path=str(tmp_path / "nao"))
    rep = ls.reconcile_sentinels(lib, root)
    assert len(rep["reconciled"]) == 1 and len(rep["invalid"]) == 1
    assert len(rep["missing_on_disk"]) == 1
    assert lib.get_album_by_source_id("tidal", "10")["download_status"] == "complete"
    assert lib.get_album(sumido)["download_status"] == "complete"  # sem --fix
    ls.reconcile_sentinels(lib, root, fix_missing=True)
    assert lib.get_album(sumido)["download_status"] == "not_downloaded"


def test_mark_unmark(tmp_path, lib):
    aid = lib.upsert_album("tidal", "1", "T", "A")
    pasta = _mk(tmp_path, "p", 1)
    ls.mark_album_downloaded(lib, aid, folder=pasta)
    assert sn.has_sentinel(pasta)
    ls.unmark_album_downloaded(lib, aid)
    assert not sn.has_sentinel(pasta)
    assert lib.get_album(aid)["download_status"] == "not_downloaded"
