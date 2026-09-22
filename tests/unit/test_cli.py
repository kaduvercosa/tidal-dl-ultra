"""Testes da CLI: parser e comandos offline (com CONFIG_DIR isolado)."""

import asyncio
import json

import pytest

from tidal_dl import cli
from tidal_dl.commands import build_parser
from tidal_dl.auth import CredentialStore, Credentials


def run(argv):
    return asyncio.run(cli.async_main(argv))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.delenv("TIDAL_DL_IOS_HOME", raising=False)
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.mark.parametrize("argv,attrs", [
    (["dl", "https://tidal.com/browse/album/1", "-q", "2", "--no-db"], {"quality": 2, "no_db": True}),
    (["dl", "u1", "u2", "--eps"], {"SOURCE": ["u1", "u2"], "eps": True}),
    (["search", "-t", "track", "daft", "punk"], {"type": "track", "QUERY": ["daft", "punk"]}),
    (["fun", "x"], {"type": "album"}),
    (["sf", "--download-new", "--every", "30", "-y"], {"download_new": True, "every": 30, "yes": True}),
    (["scan", "/x", "--dry-run"], {"DIR": "/x", "dry_run": True}),
    (["lib", "reconcile", "/m", "--fix"], {"action": "reconcile", "TARGET": "/m", "fix": True}),
    (["login", "--device"], {"device": True}),
])
def test_parser(argv, attrs):
    ns = build_parser().parse_args(argv)
    for k, v in attrs.items():
        assert getattr(ns, k) == v


def test_parser_rejeita_qualidade_invalida():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["dl", "x", "-q", "9"])


def test_sem_comando_mostra_tela_inicial(capsys):
    assert run([]) == 0
    out = capsys.readouterr().out
    for trecho in ("█", "SESSÃO:", "não logado", "PRIMEIROS PASSOS:", "COMANDOS:", "sync-favorites (sf)",
                   "FLAGS GLOBAIS:", "--no-color"):
        assert trecho in out


def test_tela_inicial_logado_nao_mostra_primeiros_passos(capsys):
    from tidal_dl.utils import get_config_paths
    import time
    CredentialStore(get_config_paths()["credentials_file"]).save(
        Credentials("t", "r", time.time() + 7200, "1", "BR", "pkce"))
    assert run([]) == 0
    out = capsys.readouterr().out
    assert "PKCE · país BR" in out and "PRIMEIROS PASSOS" not in out


def test_logo_adapta_a_tela_estreita(capsys):
    from tidal_dl import welcome
    welcome.print_logo(30)   # iPhone em pé: cai para o logo curto
    curto = capsys.readouterr().out
    welcome.print_logo(120)
    longo = capsys.readouterr().out
    assert len(curto.splitlines()[0]) < len(longo.splitlines()[0]) and len(curto.splitlines()) == 5


def test_ajuda_em_portugues(capsys):
    from tidal_dl.commands import build_parser
    build_parser().print_help()
    out = capsys.readouterr().out
    assert "opções:" in out and "mostra esta mensagem de ajuda e sai" in out and "show this help" not in out
    assert "Uso:" in out


def test_doctor_json_sem_login(capsys):
    rc = run(["doctor", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and any(d["name"] == "Login" for d in data)
    assert rc in (0, 1)  # 1 só se faltar httpx/mutagen no ambiente de teste


def test_stats_vazio_e_logout_sem_token(capsys):
    assert run(["stats"]) == 0
    assert run(["logout"]) == 0


def test_logout_apaga_token(tmp_path):
    from tidal_dl.utils import get_config_paths
    store = CredentialStore(get_config_paths()["credentials_file"])
    store.save(Credentials("t", "r"))
    assert run(["logout"]) == 0 and store.load() is None


def test_config_show_e_reset(tmp_path, capsys):
    assert run(["config", "--show"]) == 0
    assert "quality" in capsys.readouterr().out
    assert run(["config", "--reset"]) == 0


def test_config_invalido_retorna_2(tmp_path):
    from tidal_dl.utils import get_config_paths
    cfg = get_config_paths()["config_file"]
    import os
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    open(cfg, "w").write("[tidal]\nquality = 9\n")
    assert run(["dl", "https://tidal.com/browse/album/1"]) == 2


def test_dl_sem_login_retorna_1(capsys):
    assert run(["dl", "https://tidal.com/browse/album/1"]) == 1


def test_library_status_offline(tmp_path):
    assert run(["library"]) == 0
    assert run(["scan", str(tmp_path / "vazia"), "--dry-run"]) == 1  # pasta inexistente


def test_expand_sources(tmp_path):
    f = tmp_path / "l.txt"
    f.write_text("https://tidal.com/browse/album/1\n")
    assert cli._expand_sources([str(f), "https://tidal.com/browse/track/2"]) == [
        "https://tidal.com/browse/album/1", "https://tidal.com/browse/track/2"]


def _paths():
    from tidal_dl.utils import get_config_paths
    return get_config_paths()


def test_stats_com_dados(capsys):
    from tidal_dl import db
    p = _paths()["tidal_db"]
    db.mark_downloaded(p, 1, "album", quality=4, saved_path="", artist="A", album="B", release_date="2021-01-01")
    db.mark_downloaded(p, 2, "track", quality=4, file_format="FLAC", artist="A", title="T")
    assert run(["stats"]) == 0
    out = capsys.readouterr().out
    assert "Registros" in out and "FLAC" in out and "2021" in out


def test_login_pkce_grava_token(monkeypatch):
    import builtins
    from fakes import jresp, make_client
    from tidal_dl.settings import TidalDLSettings

    client, _ = make_client(lambda *a: jresp({"access_token": "A", "refresh_token": "R", "expires_in": 100,
                                              "user": {"userId": 3, "countryCode": "PT"}}))
    monkeypatch.setattr(builtins, "input", lambda prompt="": "https://tidal.com/android/login/auth?code=abc")
    ns = build_parser().parse_args(["login"])
    assert asyncio.run(cli.cmd_login(ns, TidalDLSettings(disable_keyring=True), _paths(), http=client)) == 0
    creds = CredentialStore(_paths()["credentials_file"]).load()
    assert creds.access_token == "A" and creds.country_code == "PT" and creds.auth_method == "pkce"


def test_login_pkce_url_invalida_falha(monkeypatch):
    import builtins
    from fakes import jresp, make_client
    from tidal_dl.settings import TidalDLSettings

    client, _ = make_client(lambda *a: jresp({}, 400))
    monkeypatch.setattr(builtins, "input", lambda prompt="": "https://tidal.com/sem-codigo")
    ns = build_parser().parse_args(["login"])
    assert asyncio.run(cli.cmd_login(ns, TidalDLSettings(disable_keyring=True), _paths(), http=client)) == 1
    assert CredentialStore(_paths()["credentials_file"]).load() is None


def test_login_tolera_url_errada_e_aceita_a_certa(monkeypatch, capsys):
    import builtins
    from fakes import jresp, make_client
    from tidal_dl.settings import TidalDLSettings

    respostas = iter(["https://login.tidal.com/authorize?code_challenge=x",
                      "https://tidal.com/android/login/auth?code=abc"])
    client, _ = make_client(lambda *a: jresp({"access_token": "A", "expires_in": 9, "user": {"userId": 1}}))
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(respostas))
    ns = build_parser().parse_args(["login"])
    assert asyncio.run(cli.cmd_login(ns, TidalDLSettings(disable_keyring=True), _paths(), http=client)) == 0
    assert "URL de LOGIN" in capsys.readouterr().out


def test_login_device(monkeypatch):
    from fakes import jresp, make_client
    from tidal_dl.settings import TidalDLSettings

    def h(method, url, params, data, headers):
        if url.endswith("device_authorization"):
            return jresp({"deviceCode": "dc", "userCode": "ABCD", "interval": 1, "expiresIn": 60})
        return jresp({"access_token": "A", "expires_in": 5, "user": {"userId": 1}})

    client, _ = make_client(h)
    ns = build_parser().parse_args(["login", "--device"])
    assert asyncio.run(cli.cmd_login(ns, TidalDLSettings(disable_keyring=True), _paths(), http=client)) == 0
    assert CredentialStore(_paths()["credentials_file"]).load().auth_method == "device_code"


def test_cmd_user(capsys, tmp_path):
    from fakes import jresp
    from scenario import Scenario
    from tidal_dl.core import TidalDL
    sc = Scenario(tmp_path)
    orig = sc.backend.handler

    def h(method, url, params, data, headers):
        if url.endswith("/users/7"):
            return jresp({"username": "fulano"})
        if url.endswith("/subscription"):
            return jresp({"status": "ACTIVE", "subscription": {"type": "HIFI_PLUS"}, "highestSoundQuality": "HI_RES_LOSSLESS"})
        return orig(method, url, params, data, headers)

    sc.backend.handler = h
    t = TidalDL(sc.settings, paths=_paths(), http=sc.client)
    t.api, t.downloader = sc.api, sc.downloader()
    assert asyncio.run(cli.cmd_user(t)) == 0
    out = capsys.readouterr().out
    assert "fulano" in out and "HIFI_PLUS" in out and "HI_RES_LOSSLESS" in out
