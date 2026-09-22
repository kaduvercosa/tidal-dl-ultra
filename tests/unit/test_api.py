"""Testes do cliente da API (token, paginação, letras)."""

import asyncio

import pytest

from fakes import jresp, make_client
from tidal_dl import auth
from tidal_dl.api import TidalAPI
from tidal_dl.exceptions import AuthenticationError, NonStreamable, ResourceNotFoundError


def run(c):
    return asyncio.run(c)


def make_api(handler, **kw):
    client, backend = make_client(handler)
    creds = auth.Credentials(kw.get("access", "tok"), kw.get("refresh", "ref"), kw.get("expiry", 9e12), "7", "BR")
    return TidalAPI(client, creds, on_refresh=kw.get("on_refresh")), backend


def test_envia_country_e_bearer():
    seen = {}

    def h(method, url, params, data, headers):
        seen.update(params=params, auth=headers["Authorization"], url=url)
        return jresp({"id": 1, "title": "A"})

    api, _ = make_api(h)
    a = run(api.get_album(1))
    assert a.title == "A" and seen["params"]["countryCode"] == "BR" and seen["auth"] == "Bearer tok"


def test_401_renova_uma_vez_e_repete():
    state = {"n": 0}
    saved = []

    def h(method, url, params, data, headers):
        if "oauth2/token" in url:
            return jresp({"access_token": "novo", "expires_in": 100})
        state["n"] += 1
        if headers["Authorization"] == "Bearer tok":
            return jresp({"userMessage": "expired"}, 401)
        return jresp({"id": 1, "title": "ok"})

    api, _ = make_api(h, on_refresh=saved.append)
    assert run(api.get_album(1)).title == "ok"
    assert state["n"] == 2 and api.creds.access_token == "novo" and saved[0].access_token == "novo"


def test_401_sem_refresh_token_propaga():
    api, _ = make_api(lambda *a: jresp({}, 401), refresh="")
    with pytest.raises(AuthenticationError):
        run(api.get_album(1))


def test_refresh_falho_propaga_401():
    def h(method, url, params, data, headers):
        return jresp({"error": "x"}, 400) if "oauth2/token" in url else jresp({}, 401)

    api, _ = make_api(h)
    with pytest.raises(AuthenticationError):
        run(api.get_album(1))


def test_refresh_concorrente_faz_um_so():
    tokens = []

    def h(method, url, params, data, headers):
        if "oauth2/token" in url:
            tokens.append(1)
            return jresp({"access_token": "novo", "expires_in": 100})
        return jresp({}, 401) if headers["Authorization"] == "Bearer tok" else jresp({"id": 1, "title": "x"})

    api, _ = make_api(h)

    async def go():
        await asyncio.gather(*(api.get_album(i) for i in range(4)))

    run(go())
    assert len(tokens) == 1


def test_ensure_token_renova_se_perto_de_expirar():
    import time

    def h(method, url, params, data, headers):
        return jresp({"access_token": "novo", "expires_in": 5000})

    api, _ = make_api(h, expiry=time.time() + 30)
    assert run(api.ensure_token()) is True and api.creds.access_token == "novo"
    api2, _ = make_api(h, expiry=time.time() + 99999)
    assert run(api2.ensure_token()) is False


def test_paginacao_de_itens_desembrulha_e_ignora_video():
    def entry(i, kind="track"):
        return {"type": kind, "item": {"id": i, "title": f"T{i}"}}

    def h(method, url, params, data, headers):
        off = params["offset"]
        items = [entry(off + i) for i in range(100)] if off == 0 else [entry(100), entry(101, "video")]
        return jresp({"items": items, "totalNumberOfItems": 102})

    api, backend = make_api(h)
    tracks = run(api.get_album_tracks(5))
    assert len(tracks) == 101 and tracks[-1].id == 100 and len(backend.calls) == 2


def test_artist_albums_pagina():
    def h(method, url, params, data, headers):
        return jresp({"items": [{"id": params["offset"] + 1, "title": "x"}], "totalNumberOfItems": 2,
                      "limit": 1, "offset": params["offset"]})

    api, _ = make_api(h)
    assert [a.id for a in run(api.get_artist_albums(1))] == [1, 2]


def test_search_aninhado_e_plano():
    api, _ = make_api(lambda *a: jresp({"albums": {"items": [{"id": 1}], "totalNumberOfItems": 1}}))
    assert run(api.search("albums", "x")).total == 1
    api, _ = make_api(lambda *a: jresp({"items": [{"id": 1}, {"id": 2}], "totalNumberOfItems": 2}))
    assert len(run(api.search("albums", "x")).items) == 2


def test_lyrics_nunca_levanta():
    api, _ = make_api(lambda *a: jresp({}, 404))
    assert run(api.get_lyrics(1)) == {}
    api, _ = make_api(lambda *a: jresp({}, 500))
    assert run(api.get_lyrics(1)) == {}
    api, _ = make_api(lambda *a: jresp({"lyrics": "la"}))
    assert run(api.get_lyrics(1)) == {"lyrics": "la"}


def test_playback_info_sem_manifest():
    api, _ = make_api(lambda *a: jresp({"userMessage": "sem direito"}))
    with pytest.raises(NonStreamable):
        run(api.playback_info(1, "LOSSLESS"))


def test_404_vira_resource_not_found():
    api, _ = make_api(lambda *a: jresp({"userMessage": "nao"}, 404))
    with pytest.raises(ResourceNotFoundError):
        run(api.get_track(1))


def test_favoritos_pagina():
    api, _ = make_api(lambda m, u, p, d, h: jresp({"items": [{"created": "x", "item": {"id": 1}}],
                                                   "totalNumberOfItems": 5, "limit": 1, "offset": p["offset"]}))
    page = run(api.favorite_albums_page(limit=1, offset=0))
    assert page.has_more and page.total == 5
