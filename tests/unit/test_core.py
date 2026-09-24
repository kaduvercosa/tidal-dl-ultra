"""Testes do orquestrador (TidalDL): URLs, busca, login e erros."""

import asyncio

import pytest

from scenario import Scenario
from tidal_dl import core
from tidal_dl.auth import CredentialStore, Credentials
from tidal_dl.core import TidalDL, parse_selection, read_url_file
from tidal_dl.exceptions import AuthenticationError, TidalDLException


def run(c):
    return asyncio.run(c)


def paths_for(tmp_path):
    base = tmp_path / "cfg"
    return {"credentials_file": str(base / "credentials.json"), "tidal_db": str(tmp_path / "t.db"),
            "config_file": str(base / "config.ini"), "library_db": str(base / "library.db")}


def make(tmp_path, **kw):
    sc = Scenario(tmp_path, **kw)
    paths = paths_for(tmp_path)
    CredentialStore(paths["credentials_file"]).save(Credentials("tok", "ref", 9e12, "7", "BR"))
    t = TidalDL(sc.settings, paths=paths, http=sc.client)
    run(t.initialize())
    return sc, t


def test_sem_login_levanta(tmp_path):
    sc = Scenario(tmp_path)
    t = TidalDL(sc.settings, paths=paths_for(tmp_path), http=sc.client)
    with pytest.raises(AuthenticationError):
        run(t.initialize())


def test_usar_sem_initialize(tmp_path):
    sc = Scenario(tmp_path)
    t = TidalDL(sc.settings, paths=paths_for(tmp_path), http=sc.client)
    with pytest.raises(TidalDLException):
        run(t.download_from_id(1, "album"))


def test_handle_url_album_e_invalida(tmp_path):
    sc, t = make(tmp_path)
    assert run(t.handle_url("https://tidal.com/browse/album/10")) is True
    assert run(t.handle_url("https://exemplo.com/x")) is False


def test_download_urls_conta_ok_e_falha(tmp_path):
    sc, t = make(tmp_path)
    r = run(t.download_urls(["https://tidal.com/browse/album/10", "lixo", "https://exemplo.com/album/1"]))
    assert r == {"ok": 1, "failed": 2}


def test_album_inexistente_devolve_false(tmp_path):
    sc, t = make(tmp_path)
    sc.handler_orig = sc.handler
    orig = sc.backend.handler

    def h(method, url, params, data, headers):
        if url.endswith("/albums/404"):
            from fakes import jresp
            return jresp({"userMessage": "nao"}, 404)
        return orig(method, url, params, data, headers)

    sc.backend.handler = h
    assert run(t.download_from_id(404, "album")) is False


def test_tipo_desconhecido(tmp_path):
    sc, t = make(tmp_path)
    with pytest.raises(TidalDLException):
        run(t.download_from_id(1, "mix"))


def test_video_via_url_chama_download_video(tmp_path, monkeypatch):
    sc, t = make(tmp_path)
    vistos = []

    async def fake_download_video(video_id):
        vistos.append(video_id)
        from tidal_dl.downloader import AlbumResult, TrackResult

        r = AlbumResult(video_id, "V", "A")
        r.tracks = [TrackResult(video_id, "V", success=True)]
        return r

    monkeypatch.setattr(t.downloader, "download_video", fake_download_video)
    assert run(t.handle_url("https://tidal.com/browse/video/555")) is True
    assert vistos == ["555"]


def test_faixa_e_playlist_e_artista(tmp_path):
    sc, t = make(tmp_path)
    assert run(t.handle_url("https://tidal.com/browse/track/1")) is True
    assert run(t.handle_url("https://tidal.com/browse/playlist/pl-1")) is True
    assert run(t.handle_url("https://tidal.com/browse/artist/5")) is True


def test_lucky_e_busca_vazia(tmp_path):
    sc, t = make(tmp_path)
    assert run(t.lucky("art", "album")) is True
    assert core.TidalDL.describe("album", {"id": 1, "title": "X", "artist": {"name": "A"},
                                           "releaseDate": "2020-01-01", "audioQuality": "LOSSLESS"})[1].startswith("A - X")


def test_interactive_com_selecao(tmp_path, monkeypatch):
    sc, t = make(tmp_path)

    # interactive() agora abre a TUI em tela cheia (prompt_toolkit) em vez
    # de pedir um número por input() de texto -- mocka core._tui_select
    # (mesma técnica usada nos testes do qobuz-dl-ultra) pra não precisar
    # de um terminal de verdade rodando em CI.
    async def escolhe_primeiro(title, options, is_multi=False, item_category="album"):
        return [(options[0], 0)]

    monkeypatch.setattr(core, "_tui_select", escolhe_primeiro)
    assert run(t.interactive("art", "album")) is True

    async def cancela(title, options, is_multi=False, item_category="album"):
        raise KeyboardInterrupt

    monkeypatch.setattr(core, "_tui_select", cancela)
    assert run(t.interactive("art", "album")) is False


def test_refresh_renovado_e_persistido(tmp_path):
    sc = Scenario(tmp_path)
    paths = paths_for(tmp_path)
    store = CredentialStore(paths["credentials_file"])
    import time
    store.save(Credentials("velho", "ref", time.time() + 10, "7", "BR"))
    t = TidalDL(sc.settings, paths=paths, http=sc.client)
    run(t.initialize())  # token perto de expirar: renova e grava
    assert store.load().access_token == "novo"


@pytest.mark.parametrize("text,maximum,expected", [
    ("1,3-5", 10, [1, 3, 4, 5]), ("5-3", 10, [3, 4, 5]), ("2,2,2", 5, [2]), ("0,9,99", 5, []),
    ("q", 5, []), ("", 5, []), ("abc,2", 5, [2]),
])
def test_parse_selection(text, maximum, expected):
    assert parse_selection(text, maximum) == expected


def test_read_url_file(tmp_path):
    f = tmp_path / "u.txt"
    f.write_text("# comentário\nhttps://tidal.com/browse/album/1  # inline\n\n  https://tidal.com/browse/track/2 \n")
    assert read_url_file(str(f)) == ["https://tidal.com/browse/album/1", "https://tidal.com/browse/track/2"]
