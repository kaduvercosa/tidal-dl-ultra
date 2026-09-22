"""Testes do downloader com um Tidal falso (sem rede, sem mutagen)."""

import asyncio
import os

import pytest

from scenario import Scenario
from tidal_dl import db, sentinel
from tidal_dl.downloader import quality_fields, run_limited
from tidal_dl.exceptions import AuthenticationError


def run(c):
    return asyncio.run(c)


def files(root):
    out = []
    for cur, _d, fs in os.walk(root):
        out += [os.path.relpath(os.path.join(cur, f), root) for f in fs]
    return sorted(out)


def test_quality_fields():
    assert quality_fields("HI_RES_LOSSLESS", 4) == ("FLAC", 24, "")
    assert quality_fields("HI_RES_LOSSLESS", 2) == ("FLAC", 16, "44.1")
    assert quality_fields("HI_RES_LOSSLESS", 1) == ("AAC", 16, "44.1")
    assert quality_fields("LOSSLESS", 4) == ("FLAC", 16, "44.1")  # álbum limita o pedido
    assert quality_fields("", 3)[0] == "FLAC"


def test_album_completo(tmp_path):
    sc = Scenario(tmp_path)
    res = run(sc.downloader().download_album(10))
    assert res.ok and res.successful == 2 and res.folder.endswith("Album/Art - Alb (2020) [FLAC 24]".replace("/", os.sep))
    fs = files(sc.dir)
    base = "Album/Art - Alb (2020) [FLAC 24]/"
    for name in ("01. Song1.flac", "02. Song2 (Explicit).flac", "01. Song1.lrc", "cover.jpg", ".streamrip.json"):
        assert (base + name).replace("/", os.sep) in fs
    assert not any(f.startswith("~tmp_") or "~tmp_" in f for f in fs)
    audio = open(os.path.join(res.folder, "01. Song1.flac"), "rb").read()
    assert audio[:4] == b"fLaC"  # remux Python entregou FLAC nativo
    assert res.tracks[0].bit_depth == 24 and res.tracks[0].sample_rate == 96000
    payload = sentinel.read_sentinel(res.folder)
    assert payload["source"] == "tidal" and payload["album_id"] == "10" and payload["tracks_downloaded"] == 2
    assert sentinel.validate_folder(res.folder, payload) == []
    assert db.is_downloaded(sc.db, 10, "album", 4) == res.folder


def test_segunda_execucao_pula_pelo_banco(tmp_path):
    sc = Scenario(tmp_path)
    run(sc.downloader().download_album(10))
    n = len(sc.asked)
    res2 = run(sc.downloader().download_album(10))
    assert res2.skipped and res2.ok and len(sc.asked) == n


def test_sem_banco_pula_faixas_existentes(tmp_path):
    sc = Scenario(tmp_path, no_database=True)
    run(sc.downloader().download_album(10))
    n = len(sc.asked)
    res = run(sc.downloader().download_album(10))
    assert res.ok and all(t.skipped for t in res.tracks) and len(sc.asked) == n


def test_falha_marca_incomplete_e_retoma(tmp_path):
    sc = Scenario(tmp_path, retries=1)
    sc.forbidden = {(2, q) for q in ("HI_RES_LOSSLESS", "HI_RES", "LOSSLESS", "HIGH", "LOW")}
    res = run(sc.downloader().download_album(10))
    assert not res.ok and res.successful == 1 and res.failed == 1
    assert os.path.basename(res.folder).startswith("[INCOMPLETE] ")
    assert not sentinel.has_sentinel(res.folder) and db.is_downloaded(sc.db, 10, "album") is None
    assert "sem permissão" in res.tracks[1].error
    # o problema some: retoma, reaproveitando a faixa 1
    sc.forbidden.clear()
    before = [a for a in sc.asked if a[0] == 1]
    res2 = run(sc.downloader().download_album(10))
    assert res2.ok and res2.tracks[0].skipped and not res2.tracks[1].skipped
    assert not os.path.basename(res2.folder).startswith("[")
    assert [a for a in sc.asked if a[0] == 1] == before
    assert sentinel.has_sentinel(res2.folder)
    assert not [f for f in files(sc.dir) if "[INCOMPLETE]" in f or "[IN PROGRESS]" in f]


def test_fallback_de_qualidade_quando_protegido(tmp_path):
    sc = Scenario(tmp_path)
    sc.protected = {(1, "HI_RES_LOSSLESS"), (1, "HI_RES")}
    res = run(sc.downloader().download_album(10))
    assert res.ok and res.tracks[0].quality == "LOSSLESS" and res.tracks[1].quality == "HI_RES_LOSSLESS"
    assert open(res.tracks[0].path, "rb").read()[:4] == b"fLaC"
    rec = db.get_record(sc.db, 10, "album")
    assert rec["quality"] == 2  # menor tier entregue


def test_no_fallback_falha_a_faixa(tmp_path):
    sc = Scenario(tmp_path, allow_quality_fallback=False, retries=1)
    sc.protected = {(1, "HI_RES_LOSSLESS")}
    res = run(sc.downloader().download_album(10))
    assert not res.ok and not res.tracks[0].success and res.tracks[1].success
    assert [a for a in sc.asked if a[0] == 1] == [(1, "HI_RES_LOSSLESS")]


def test_retry_de_rede_recupera_segmento(tmp_path):
    sc = Scenario(tmp_path, tracks=1, retries=3)
    sc.fail_once = {"https://cdn/1/seg1.m4s"}
    res = run(sc.downloader().download_album(10))
    assert res.ok


def test_erro_fatal_aborta_e_deixa_incomplete(tmp_path):
    sc = Scenario(tmp_path, tracks=3, concurrency=1)
    sc.status401_tracks = {2}
    sc.creds.refresh_token = ""  # não há como renovar
    sc.api.creds.refresh_token = ""
    with pytest.raises(AuthenticationError):
        run(sc.downloader().download_album(10))
    folders = [d for d in os.listdir(os.path.join(sc.dir, "Album"))]
    assert folders == ["[INCOMPLETE] Art - Alb (2020) [FLAC 24]"]
    assert not any("~tmp_" in f for f in files(sc.dir))


def test_multi_disco_usa_subpastas_cd(tmp_path):
    sc = Scenario(tmp_path, tracks=2, volumes=2)
    res = run(sc.downloader().download_album(10))
    assert res.ok
    fs = files(res.folder)
    assert any(f.startswith("CD 01") for f in fs) and any(f.startswith("CD 02") for f in fs)
    assert any("1.01 - Song1.flac" in f.replace(os.sep, "/") or "01.01 - Song1.flac" in f for f in fs)


def test_faixa_avulsa_vai_para_pasta_do_album_e_dedup(tmp_path):
    sc = Scenario(tmp_path)
    dl = sc.downloader()
    res = run(dl.download_track(2))
    assert res.ok and os.path.basename(res.tracks[0].path) == "02. Song2 (Explicit).flac"
    assert not sentinel.has_sentinel(res.folder)  # faixa avulsa não vira álbum "completo"
    res2 = run(sc.downloader().download_track(2))
    assert res2.skipped


def test_playlist_numera_e_gera_m3u8(tmp_path):
    sc = Scenario(tmp_path)
    res = run(sc.downloader().download_playlist("pl-1"))
    assert res.ok
    fs = files(res.folder)
    assert "01. Art - Song1.flac" in fs and "02. Art - Song2.flac" in fs
    m3u = open(os.path.join(res.folder, "Minha Lista.m3u8"), encoding="utf-8").read().splitlines()
    assert m3u[0] == "#EXTM3U" and len(m3u) == 3


def test_remux_none_mantem_mp4(tmp_path):
    sc = Scenario(tmp_path, tracks=1, remux="none")
    res = run(sc.downloader().download_album(10))
    assert res.ok and res.tracks[0].path.endswith(".mp4")


def test_remux_python_falho_sem_ffmpeg_falha_a_faixa(tmp_path, monkeypatch):
    sc = Scenario(tmp_path, tracks=1, remux="python", retries=1)
    sc.backend.files["https://cdn/1/init.mp4"] = b"lixo sem moov"
    res = run(sc.downloader().download_album(10))
    assert not res.ok and "áudio" in res.tracks[0].error
    assert os.path.basename(res.folder).startswith("[INCOMPLETE]")


def test_run_limited_respeita_limite_e_ordem():
    active, peak = [0], [0]

    async def job(i):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0)
        active[0] -= 1
        return i

    out = run(run_limited([job(i) for i in range(6)], 2))
    assert out == list(range(6)) and peak[0] <= 2


def test_run_limited_cancela_restantes_no_erro():
    done = []

    async def ok(i):
        await asyncio.sleep(0.05)
        done.append(i)

    async def boom():
        raise AuthenticationError("x")

    with pytest.raises(AuthenticationError):
        run(run_limited([boom(), ok(1), ok(2)], 3))
    assert done == []
