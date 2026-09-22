"""Testes dos utilitários puros."""

import asyncio
import os

import pytest

from tidal_dl import utils as u


@pytest.mark.parametrize("url,expected", [
    ("https://tidal.com/browse/album/123", ("album", "123")),
    ("https://listen.tidal.com/album/1/track/2", ("track", "2")),
    ("tidal.com/track/9", ("track", "9")),
    ("https://tidal.com/browse/playlist/abcd-1234", ("playlist", "abcd-1234")),
    ("tidal://album/55", ("album", "55")),
    ("https://tidal.com/browse/artist/77?x=1", ("artist", "77")),
    ("https://open.spotify.com/album/1", None),
    ("https://tidal.com/browse/mix/abc", None),
    ("https://tidal.com/browse/album/abc", None),
    ("", None),
])
def test_parse_url(url, expected):
    assert u.parse_url(url) == expected


def test_sanitize_component():
    assert u.sanitize_component("AC/DC: Back In Black?.") == "AC - DC Back In Black"
    assert u.sanitize_component("CON") == "_CON"
    assert u.sanitize_component("   ") == "_"
    assert len(u.sanitize_component("x" * 500)) == 200


def test_render_path_sanitiza_valores_e_mantem_vazios():
    out = u.render_path("{a}/{b}{c}", {"a": "X/Y", "b": "z", "c": ""}, "fb")
    assert out == os.path.join("X - Y", "z")


def test_render_path_fallback_em_chave_desconhecida():
    assert u.render_path("{nao_existe}", {"a": "1"}, "padrao") == "padrao"


def test_validate_template():
    assert u.validate_template("{a} {b}", {"a", "b"}) == []
    assert u.validate_template("{x}", {"a"}) == ["x"]
    assert u.validate_template("{a", {"a"})[0].startswith("template inválido")


def test_truncate_name():
    assert u.truncate_name("a" * 300, 100, ".flac") == "a" * 95
    assert u.truncate_name("curto", 100) == "curto"


def test_cover_url():
    assert u.cover_url("aa-bb-cc", 640) == "https://resources.tidal.com/images/aa/bb/cc/640x640.jpg"
    assert u.cover_url(None) is None


def test_human():
    assert u.human_size(512) == "512 B" and u.human_size(2048) == "2.0 KB"
    assert u.human_duration(125) == "2:05" and u.human_duration(3725) == "1:02:05"
    assert u.human_duration(None) == "--:--"


def test_config_paths_por_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TIDAL_DL_IOS_HOME", str(tmp_path))
    monkeypatch.delenv("CONFIG_DIR", raising=False)
    p = u.get_config_paths()
    assert p["config_file"] == str(tmp_path / "tidal-dl" / "config.ini")
    assert u.is_ios() and u.default_download_folder() == str(tmp_path / "TidalDownloads")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "x"))
    assert u.get_config_paths()["config_dir"] == str(tmp_path / "x")


def test_config_paths_autodeteccao_ashell(monkeypatch):
    monkeypatch.delenv("TIDAL_DL_IOS_HOME", raising=False)
    monkeypatch.delenv("CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", "/private/var/mobile/Containers/Data/Application/ABC/Documents/..")
    assert u.is_ios()
    assert u.get_config_paths()["config_dir"].endswith("/Documents")


def test_retry_async_sucesso_apos_falhas():
    calls = []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ValueError("x")
        return "ok"

    async def nosleep(_):
        pass

    assert asyncio.run(u.retry_async(fn, attempts=3, sleep=nosleep)) == "ok"


def test_retry_async_esgota_e_relevanta():
    async def fn():
        raise KeyError("k")

    async def nosleep(_):
        pass

    with pytest.raises(KeyError):
        asyncio.run(u.retry_async(fn, attempts=2, sleep=nosleep))


def test_retry_async_respeita_retry_after():
    waited = []

    class E(Exception):
        retry_after = 7

    async def fn():
        raise E()

    async def sleep(s):
        waited.append(s)

    with pytest.raises(E):
        asyncio.run(u.retry_async(fn, attempts=2, base_delay=1, sleep=sleep))
    assert waited == [7.0]


def test_atomic_write_e_leftovers(tmp_path):
    p = str(tmp_path / "sub" / "a.txt")
    u.atomic_write_text(p, "oi", mode=0o600)
    assert open(p).read() == "oi"
    (tmp_path / "~tmp_1.part").write_bytes(b"x")
    assert u.clean_leftovers(str(tmp_path), "~tmp_") == 1
