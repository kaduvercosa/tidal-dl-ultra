"""Testes de autenticação (PKCE, device-code, refresh, armazenamento)."""

import asyncio
import base64
import json
import os
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from fakes import jresp, make_client
from tidal_dl import auth
from tidal_dl.exceptions import AuthenticationError
from tidal_dl.net import Response


def run(c):
    return asyncio.run(c)


def test_pkce_pair_e_url():
    v, c, k = auth.generate_pkce_pair()
    import hashlib
    expect = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert c == expect and len(k) == 16
    url = auth.build_pkce_authorize_url(c, k)
    assert "%" not in url  # sem %: nada para o terminal/navegador recodificar (%3A -> %253A)
    assert "redirect_uri=https://tidal.com/android/login/auth" in url
    q = parse_qs(urlsplit(url).query)
    assert q["redirect_uri"] == [auth.PKCE_REDIRECT_URI]
    assert q["code_challenge"] == [c] and q["client_unique_key"] == [k]
    assert q["code_challenge_method"] == ["S256"] and q["client_id"] == [auth.CLIENT_ID_PKCE]


@pytest.mark.parametrize("value,code", [
    ("https://tidal.com/android/login/auth?code=abc123&state=x", "abc123"),
    ("abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz"),
])
def test_extract_code(value, code):
    assert auth.extract_code_from_redirect(value) == code


@pytest.mark.parametrize("bad", ["", "   ", "https://tidal.com/x", "curto"])
def test_extract_code_invalido(bad):
    with pytest.raises(ValueError):
        auth.extract_code_from_redirect(bad)


def test_exchange_pkce_ok_e_erro():
    def ok(method, url, params, data, headers):
        assert data["grant_type"] == "authorization_code" and data["code_verifier"] == "ver"
        return jresp({"access_token": "A", "refresh_token": "R", "expires_in": 3600,
                      "user": {"userId": 9, "countryCode": "BR"}})

    client, _ = make_client(ok)
    c = run(auth.exchange_pkce_code(client, "code", "ver", "key"))
    assert (c.access_token, c.refresh_token, c.user_id, c.country_code, c.auth_method) == ("A", "R", "9", "BR", "pkce")
    assert 3500 < c.expires_in() <= 3600

    client, _ = make_client(lambda *a: jresp({"error_description": "invalid code"}, 400))
    with pytest.raises(AuthenticationError):
        run(auth.exchange_pkce_code(client, "x", "v", "k"))


def test_login_pkce_guiado():
    shown = []

    def handler(method, url, params, data, headers):
        return jresp({"access_token": "A", "expires_in": 10, "user": {"userId": 1}})

    client, _ = make_client(handler)

    async def ask():
        return "https://tidal.com/android/login/auth?code=zzz"

    c = run(auth.login_pkce(client, show_url=shown.append, ask_redirect=ask))
    assert c.access_token == "A" and shown and shown[0].startswith(auth.PKCE_AUTHORIZE_URL)


def test_refresh_pkce_usa_client_secret_e_preserva_refresh():
    seen = {}

    def handler(method, url, params, data, headers):
        seen.update(data)
        seen["auth"] = headers.get("Authorization")
        return jresp({"access_token": "N", "expires_in": 100})

    client, _ = make_client(handler)
    old = auth.Credentials("O", "R1", 1, "5", "BR", auth.METHOD_PKCE)
    new = run(auth.refresh_credentials(client, old))
    assert new.access_token == "N" and new.refresh_token == "R1" and new.user_id == "5"
    assert seen["client_id"] == auth.CLIENT_ID_PKCE and "client_secret" in seen and seen["auth"] is None


def test_refresh_device_usa_basic_auth():
    seen = {}

    def handler(method, url, params, data, headers):
        seen["auth"] = headers.get("Authorization")
        seen["cid"] = data["client_id"]
        return jresp({"access_token": "N", "refresh_token": "R2", "expires_in": 100})

    client, _ = make_client(handler)
    new = run(auth.refresh_credentials(client, auth.Credentials("O", "R1", auth_method=auth.METHOD_DEVICE)))
    assert seen["auth"].startswith("Basic ") and seen["cid"] == auth.CLIENT_ID and new.refresh_token == "R2"


def test_refresh_sem_refresh_token_ou_erro():
    client, _ = make_client(lambda *a: jresp({"error": "x"}, 400))
    with pytest.raises(AuthenticationError):
        run(auth.refresh_credentials(client, auth.Credentials("O", "")))
    with pytest.raises(AuthenticationError):
        run(auth.refresh_credentials(client, auth.Credentials("O", "R")))


def test_is_stale():
    assert auth.Credentials("a", token_expiry=time.time() + 10).is_stale(300)
    assert not auth.Credentials("a", token_expiry=time.time() + 9999).is_stale(300)
    assert not auth.Credentials("a", token_expiry=0).is_stale(300)


def test_login_device_polling_ate_autorizar():
    polls = [Response(400, {}, json.dumps({"status": 400, "sub_status": 1002}).encode()),
             Response(400, {}, json.dumps({"error": "authorization_pending"}).encode()),
             jresp({"access_token": "A", "refresh_token": "R", "expires_in": 5, "user": {"userId": 2}})]

    def handler(method, url, params, data, headers):
        if url.endswith("device_authorization"):
            return jresp({"deviceCode": "dc", "userCode": "ABCD", "verificationUriComplete": "link.tidal.com/ABCD",
                          "interval": 1, "expiresIn": 300})
        return polls.pop(0)

    client, _ = make_client(handler)
    shown, sleeps = [], []

    async def sleep(s):
        sleeps.append(s)

    c = run(auth.login_device(client, show_code=lambda u, code: shown.append((u, code)), sleep=sleep))
    assert c.auth_method == "device_code" and shown == [("https://link.tidal.com/ABCD", "ABCD")] and len(sleeps) == 2


def test_login_device_recusado_e_expirado():
    def handler(method, url, params, data, headers):
        if url.endswith("device_authorization"):
            return jresp({"deviceCode": "dc", "userCode": "X", "interval": 1, "expiresIn": 300})
        return jresp({"error": "access_denied"}, 400)

    client, _ = make_client(handler)

    async def sleep(_):
        pass

    with pytest.raises(AuthenticationError):
        run(auth.login_device(client, show_code=lambda *a: None, sleep=sleep))

    ticks = iter([0, 1000])  # relógio pula o prazo
    def handler2(method, url, params, data, headers):
        if url.endswith("device_authorization"):
            return jresp({"deviceCode": "dc", "userCode": "X", "interval": 1, "expiresIn": 5})
        return jresp({}, 400)

    client, _ = make_client(handler2)
    with pytest.raises(AuthenticationError):
        run(auth.login_device(client, show_code=lambda *a: None, sleep=sleep, clock=lambda: next(ticks)))


def test_store_arquivo_0600_roundtrip_e_delete(tmp_path):
    path = str(tmp_path / "sub" / "credentials.json")
    store = auth.CredentialStore(path)
    creds = auth.Credentials("tok", "ref", 123.0, "7", "BR", auth.METHOD_PKCE)
    assert store.save(creds) == path
    if os.name == "posix":
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    back = store.load()
    assert back.access_token == "tok" and back.auth_method == "pkce" and back.user_id == "7"
    assert store.delete() is True and store.load() is None and store.delete() is False


def test_store_ignora_arquivo_corrompido_ou_sem_token(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{quebrado")
    assert auth.CredentialStore(str(p)).load() is None
    p.write_text(json.dumps({"access_token": ""}))
    assert auth.CredentialStore(str(p)).load() is None


def test_url_de_login_colada_por_engano_tem_mensagem_propria():
    url = auth.build_pkce_authorize_url("chal", "key")
    for bad in (url, url.replace("https://tidal", "https%253A%252F%252Ftidal")):
        with pytest.raises(ValueError) as e:
            auth.extract_code_from_redirect(bad)
        assert "URL de LOGIN" in str(e.value)
    with pytest.raises(ValueError) as e:
        auth.extract_code_from_redirect("https://login.tidal.com/authorize?redirect_uri=https%253A%252F%252Fx&a=1")
    assert "%25" in str(e.value)


def test_deep_link_do_app_tambem_serve():
    assert auth.extract_code_from_redirect("tidal://login/auth?code=abc123") == "abc123"


def test_login_pkce_repete_pergunta_sem_trocar_o_par_pkce():
    shown, warned, answers = [], [], iter([
        "https://login.tidal.com/authorize?code_challenge=x",
        "",
        "https://tidal.com/android/login/auth?code=ok123",
    ])
    seen = {}

    def handler(method, url, params, data, headers):
        seen["verifier"] = data["code_verifier"]
        return jresp({"access_token": "A", "expires_in": 9, "user": {"userId": 1}})

    client, _ = make_client(handler)

    async def ask():
        return next(answers)

    c = run(auth.login_pkce(client, show_url=shown.append, ask_redirect=ask, on_error=warned.append))
    assert c.access_token == "A" and len(shown) == 1 and len(warned) == 2  # uma URL só, dois avisos


def test_login_pkce_desiste_apos_tentativas():
    client, _ = make_client(lambda *a: jresp({}, 400))

    async def ask():
        return "lixo"

    with pytest.raises(ValueError):
        run(auth.login_pkce(client, show_url=lambda u: None, ask_redirect=ask, attempts=2))
