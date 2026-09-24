"""Testes do downloader com um Tidal falso (sem rede, sem mutagen)."""

import asyncio
import os

import pytest

from scenario import Scenario
from tidal_dl import db, sentinel
from tidal_dl.downloader import effective_quality, quality_fields, run_limited
from tidal_dl.exceptions import AuthenticationError
from tidal_dl.models import Album, Artist


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


def test_album_usa_o_maximo_publicado_sem_fallback_artificial(tmp_path):
    sc = Scenario(tmp_path, quality="LOSSLESS")
    res = run(sc.downloader().download_album(10))
    assert res.ok
    assert effective_quality(Album(10, "Alb", artist=Artist(), audio_quality="LOSSLESS"), 4) == 2
    assert all(tier == "LOSSLESS" for _tid, tier in sc.asked)
    assert res.folder.endswith("[FLAC 16]")


def test_video_de_album_tem_raiz_separada_mesmo_com_configuracao_antiga(tmp_path):
    sc = Scenario(tmp_path)
    sc.settings.video_directory = sc.settings.directory
    dl = sc.downloader()
    album = Album(10, "Alb", artist=Artist(name="Art"), release_date="2020-01-02")
    video_folder = dl.album_video_folder(album)
    assert video_folder.startswith(os.path.join(sc.settings.directory, "Videos"))
    assert os.path.commonpath((video_folder, sc.settings.directory)) == sc.settings.directory
    assert os.path.commonpath((video_folder, sc.settings.directory)) != video_folder


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
    sc = Scenario(tmp_path, tracks=3, max_workers=1)
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


# ---------------------------------------------------------------------------
# Modos (sequencial/paralelo), barras de progresso, retomada e resumo
# ---------------------------------------------------------------------------

from tidal_dl import progress  # noqa: E402

PLAIN = b"fLaC" + b"P" * 200


class Spy:
    """tqdm falso: registra como cada barra foi criada."""

    instances = []

    def __init__(self, **kw):
        self.kw, self.n = kw, kw.get("initial", 0)
        Spy.instances.append(self)

    def update(self, n=1):
        self.n += n

    def set_postfix_str(self, s):
        pass

    def close(self):
        pass


def spy(monkeypatch):
    Spy.instances = []
    monkeypatch.setattr(progress, "_load_tqdm", lambda: Spy)


def test_sequencial_dash_mostra_modo_barra_e_montagem(tmp_path, monkeypatch, capsys):
    spy(monkeypatch)
    sc = Scenario(tmp_path)  # padrão: max_workers=1
    res = run(sc.downloader().download_album(10))
    out = capsys.readouterr().out
    assert res.ok
    assert "Sequencial" in out and "Paralelo" not in out
    assert "Em Progresso: [1/2] Song1" in out and "└─ Concluído: [1/2] Song1" in out
    assert "Montando o arquivo FLAC final" in out
    assert len(Spy.instances) == 2 and all(b.kw["unit"] == " seg" and b.kw["total"] == 3 for b in Spy.instances)
    assert "RESUMO DA ÁLBUM" in out and "2/2" in out


def test_sequencial_bts_barra_em_bytes(tmp_path, monkeypatch, capsys):
    spy(monkeypatch)
    sc = Scenario(tmp_path, quality="LOSSLESS", quality_=None) if False else Scenario(tmp_path, quality="LOSSLESS")
    sc.settings.quality = 2
    res = run(sc.downloader().download_album(10))
    assert res.ok and open(res.tracks[0].path, "rb").read() == PLAIN
    kws = [b.kw for b in Spy.instances]
    assert len(kws) == 2 and all(k["unit"] == "iB" and k["total"] == len(PLAIN) for k in kws)


def test_paralelo_sem_barras_mas_com_linhas_de_progresso(tmp_path, monkeypatch, capsys):
    spy(monkeypatch)
    sc = Scenario(tmp_path, max_workers=2)
    res = run(sc.downloader().download_album(10))
    out = capsys.readouterr().out
    assert res.ok and "Paralelo (2 workers)" in out
    assert Spy.instances == []
    assert "Em Progresso: [1/2] Song1 [3 segmentos]" in out and "└─ Concluído: [2/2] Song2" in out
    assert "Montando o arquivo FLAC final" not in out


def test_paralelo_bts_mostra_tamanho(tmp_path, capsys):
    sc = Scenario(tmp_path, quality="LOSSLESS", max_workers=2)
    sc.settings.quality = 2
    run(sc.downloader().download_album(10))
    assert "Em Progresso: [1/2] Song1 [0.0 MB]" in capsys.readouterr().out


def test_delay_forca_sequencial_e_espera_entre_faixas(tmp_path, capsys):
    waited = []

    async def sleep(s):
        waited.append(s)

    sc = Scenario(tmp_path, max_workers=4, delay=0.5)
    res = run(sc.downloader(sleep=sleep).download_album(10))
    assert res.ok and "Sequencial (Safety Delay ativo)" in capsys.readouterr().out
    assert waited == [0.5, 0.5]


def test_no_progress_desliga_a_barra(tmp_path, monkeypatch):
    spy(monkeypatch)
    sc = Scenario(tmp_path, progress_bar=False)
    assert run(sc.downloader().download_album(10)).ok and Spy.instances == []


def test_faixa_avulsa_mostra_cabecalho_e_modo(tmp_path, capsys):
    sc = Scenario(tmp_path)
    run(sc.downloader().download_track(1))
    out = capsys.readouterr().out
    assert "FAIXA" in out and "Sequencial" in out and "Song1" in out


def test_retomada_bts_com_range_apos_queda(tmp_path, capsys):
    sc = Scenario(tmp_path, tracks=1, quality="LOSSLESS", retries=3)
    sc.settings.quality = 2
    sc.backend.cut["https://cdn/1/plain.flac"] = 100
    res = run(sc.downloader().download_album(10))
    assert res.ok and open(res.tracks[0].path, "rb").read() == PLAIN  # sem bytes duplicados
    ranges = [(c[4] or {}).get("Range") for c in sc.backend.calls if c[0] == "STREAM" and c[1].endswith("plain.flac")]
    assert ranges == [None, "bytes=100-"]
    assert "Falha de Rede. Tentativa 2/3" in capsys.readouterr().out


def test_servidor_que_ignora_range_recomeca_do_zero(tmp_path):
    sc = Scenario(tmp_path, tracks=1, quality="LOSSLESS", retries=3)
    sc.settings.quality = 2
    sc.backend.honor_range = False
    sc.backend.cut["https://cdn/1/plain.flac"] = 100
    res = run(sc.downloader().download_album(10))
    assert res.ok and open(res.tracks[0].path, "rb").read() == PLAIN


def test_retomada_dash_continua_no_segmento_que_faltava(tmp_path):
    sc = Scenario(tmp_path, tracks=1, retries=3)
    sc.backend.cut["https://cdn/1/seg2.m4s"] = 3
    res = run(sc.downloader().download_album(10))
    assert res.ok and open(res.tracks[0].path, "rb").read()[:4] == b"fLaC"
    hits = lambda name: sum(1 for c in sc.backend.calls if c[0] == "STREAM" and c[1].endswith(name))
    assert hits("init.mp4") == 1 and hits("seg1.m4s") == 1 and hits("seg2.m4s") == 2


def test_erro_permanente_nao_repete(tmp_path, capsys):
    sc = Scenario(tmp_path, tracks=1, quality="LOSSLESS", retries=5)
    sc.settings.quality = 2
    del sc.backend.files["https://cdn/1/plain.flac"]
    res = run(sc.downloader().download_album(10))
    assert not res.ok and "HTTP 404" in res.tracks[0].error
    assert sum(1 for c in sc.backend.calls if c[0] == "STREAM") == 1
    assert "Falha de Rede" not in capsys.readouterr().out


def test_resumo_mostra_puladas_falhas_e_fallback(tmp_path, capsys):
    sc = Scenario(tmp_path, retries=1)
    run(sc.downloader().download_album(10))          # 1ª vez: tudo baixado
    capsys.readouterr()
    sc.protected = set()
    (tmp_path / "x").mkdir()
    import shutil
    shutil.rmtree(os.path.join(sc.dir, "Album"))     # apaga tudo e refaz com fallback + falha
    sc.protected = {(1, "HI_RES_LOSSLESS")}
    sc.forbidden = {(2, q) for q in ("HI_RES_LOSSLESS", "HI_RES", "LOSSLESS", "HIGH", "LOW")}
    import tidal_dl.db as dbm
    dbm.purge(sc.db)
    run(sc.downloader().download_album(10))
    out = capsys.readouterr().out
    assert "Baixadas com sucesso : " in out and "1/2" in out
    assert "Em qualidade menor (fallback) : " in out and "Falhas : " in out


def test_abort_por_ctrl_c_limpa_temporarios(tmp_path):
    from tidal_dl.downloader import Downloader

    class Aborta(Downloader):
        async def _fetch_segment(self, url):
            data = await super()._fetch_segment(url)
            if url.endswith("seg1.m4s"):
                progress.abort_event.set()
            return data

    sc = Scenario(tmp_path, tracks=1)
    with pytest.raises(KeyboardInterrupt):
        run(Aborta(sc.api, sc.settings, db_path=sc.db).download_album(10))
    assert not any("~tmp_" in f for f in files(sc.dir))
    assert db.is_downloaded(sc.db, 10, "album") is None


def test_fallback_lrclib_quando_tidal_nao_tem_letra(tmp_path):
    from fakes import jresp

    sc = Scenario(tmp_path, tracks=1)
    orig = sc.backend.handler

    def handler(method, url, params, data, headers):
        if url.endswith("/lyrics"):
            return jresp({}, 404)  # Tidal sem letra
        if url == "https://lrclib.net/api/get":
            return jresp({"plainLyrics": "letra de reforco", "syncedLyrics": ""})
        return orig(method, url, params, data, headers)

    sc.backend.handler = handler
    res = run(sc.downloader().download_album(10))
    assert res.ok
    lrc_or_txt = res.tracks[0].path.rsplit(".", 1)[0]
    from tidal_dl import metadata as _md  # só para import válido no bloco
    # confere que a letra de fallback foi de fato usada nas tags do FLAC (mutagen ausente:
    # o efeito observável aqui é indireto -- então validamos via chamada registrada)
    assert any(c[1] == "https://lrclib.net/api/get" for c in sc.backend.calls)


def test_sem_fallback_lrclib_quando_desligado(tmp_path):
    from fakes import jresp

    sc = Scenario(tmp_path, tracks=1, lyrics_fallback=False)
    orig = sc.backend.handler

    def handler(method, url, params, data, headers):
        if url.endswith("/lyrics"):
            return jresp({}, 404)
        return orig(method, url, params, data, headers)

    sc.backend.handler = handler
    res = run(sc.downloader().download_album(10))
    assert res.ok
    assert not any(c[1] == "https://lrclib.net/api/get" for c in sc.backend.calls)
