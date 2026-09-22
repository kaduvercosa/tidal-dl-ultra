"""Testes da sentinela .streamrip.json (dedup pelo disco)."""

import json
import os

import pytest

from tidal_dl import sentinel as sn


def _album(tmp_path, name="A - B", n=2):
    d = tmp_path / name
    d.mkdir(parents=True)
    for i in range(n):
        (d / f"{i + 1:02}.flac").write_bytes(b"x")
    return d


def test_escrita_leitura_e_remocao(tmp_path):
    d = _album(tmp_path)
    payload = sn.build_payload("tidal", 123, "B", "A", 2, [
        {"id": "1", "success": True}, {"id": "2", "success": True}])
    assert sn.write_sentinel(d, payload) is True
    assert sn.has_sentinel(d)
    lido = sn.read_sentinel(d)
    assert lido["album_id"] == "123" and lido["tracks_downloaded"] == 2
    assert not (d / (sn.SENTINEL_FILENAME + ".tmp")).exists()
    assert sn.remove_sentinel(d) is True
    assert sn.remove_sentinel(d) is False


def test_escrita_best_effort_em_pasta_inexistente(tmp_path):
    assert sn.write_sentinel(tmp_path / "nao_existe", {"a": 1}) is False


def test_leitura_de_json_invalido_ou_nao_objeto(tmp_path):
    (tmp_path / sn.SENTINEL_FILENAME).write_text("{quebrado")
    assert sn.read_sentinel(tmp_path) is None
    (tmp_path / sn.SENTINEL_FILENAME).write_text("[1,2]")
    assert sn.read_sentinel(tmp_path) is None


def test_identidade_sem_source_assume_qobuz():
    assert sn.sentinel_identity({"album_id": 9}) == ("qobuz", "9")


@pytest.mark.parametrize("payload", [
    {"source": "spotify", "album_id": "1"},
    {"source": 3, "album_id": "1"},
    {"album_id": ""},
    {"album_id": True},
    {"album_id": None},
])
def test_identidade_invalida(payload):
    with pytest.raises(sn.SentinelValidationError):
        sn.sentinel_identity(payload)


def test_downloaded_at_preserva_valido_e_repoe_invalido():
    assert sn.sentinel_downloaded_at({"downloaded_at": "2024-01-02T03:04:05+00:00"}) == "2024-01-02T03:04:05+00:00"
    assert sn.sentinel_downloaded_at({"downloaded_at": "ontem"}).startswith("20")


def test_descoberta_ignora_symlink_para_fora_e_json_ruim(tmp_path):
    root = tmp_path / "lib"
    ok = _album(root, "ok")
    sn.write_sentinel(ok, sn.build_payload("tidal", "1", "t", "a", 2))
    ruim = _album(root, "ruim")
    (ruim / sn.SENTINEL_FILENAME).write_text("{")
    fora = _album(tmp_path, "fora")
    sn.write_sentinel(fora, sn.build_payload("tidal", "2", "t", "a", 2))
    try:
        os.symlink(fora, root / "atalho")
    except (OSError, NotImplementedError):
        pytest.skip("sem suporte a symlink")
    records, failures, scanned = sn.discover_sentinels(root)
    assert [r.payload["album_id"] for r in records] == ["1"]
    assert len(failures) == 2  # JSON ruim + symlink para fora
    assert scanned >= 3


def test_descoberta_raiz_inexistente(tmp_path):
    assert sn.discover_sentinels(tmp_path / "x") == ([], [], 0)


def test_validate_folder_consistente_e_parcial(tmp_path):
    d = _album(tmp_path, n=2)
    tracks = [{"id": "1", "success": True}, {"id": "2", "success": True}]
    assert sn.validate_folder(d, sn.build_payload("tidal", "1", "t", "a", 2, tracks)) == []
    # falta arquivo
    (d / "02.flac").unlink()
    assert any("achou 1" in p for p in sn.validate_folder(d, sn.build_payload("tidal", "1", "t", "a", 2, tracks)))
    # faixa marcada como falha
    parcial = sn.build_payload("tidal", "1", "t", "a", 2, [
        {"id": "1", "success": True}, {"id": "2", "success": False}])
    assert any("parcial" in p for p in sn.validate_folder(d, parcial))


def test_validate_folder_contagem_invalida(tmp_path):
    assert sn.validate_folder(tmp_path, {"tracks_count": "abc"}) == ["tracks_count inválido"]
    assert sn.validate_folder(tmp_path, {"tracks_count": True}) == ["tracks_count inválido"]


def test_count_audio_ignora_symlink_e_conta_discos(tmp_path):
    d = tmp_path / "alb"
    (d / "CD 01").mkdir(parents=True)
    (d / "CD 01" / "1.flac").write_bytes(b"x")
    (d / "capa.jpg").write_bytes(b"x")
    assert sn.count_audio_files(d) == 1
