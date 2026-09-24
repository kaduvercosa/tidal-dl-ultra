"""Testes do downloader HLS (master/media playlist, AES-128, fMP4, retry)."""

import asyncio
import os

import pytest

from fakes import make_client
from tidal_dl import hls
from tidal_dl.exceptions import DownloadError, PermanentDownloadError


def run(c):
    return asyncio.run(c)


MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
low/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720
mid/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
high/index.m3u8
"""

MEDIA_PLAIN = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:4.0,
seg0.ts
#EXTINF:4.0,
seg1.ts
#EXT-X-ENDLIST
"""


def test_is_master_e_parse_variantes():
    assert hls.is_master_playlist(MASTER)
    variants = hls.parse_master_playlist(MASTER, "https://cdn/master.m3u8")
    assert [v.bandwidth for v in variants] == [800000, 3000000, 6000000]
    assert variants[0].url == "https://cdn/low/index.m3u8"


@pytest.mark.parametrize("quality,expected_bw", [("LOW", 800000), ("MEDIUM", 3000000), ("HIGH", 6000000)])
def test_pick_variant(quality, expected_bw):
    variants = hls.parse_master_playlist(MASTER, "https://cdn/master.m3u8")
    assert hls.pick_variant(variants, quality).bandwidth == expected_bw


def test_master_sem_variantes_falha():
    with pytest.raises(DownloadError):
        hls.parse_master_playlist("#EXTM3U\n#EXT-X-VERSION:3\n", "https://cdn/x")


def test_media_playlist_simples():
    pl = hls.parse_media_playlist(MEDIA_PLAIN, "https://cdn/high/")
    assert [s.url for s in pl.segments] == ["https://cdn/high/seg0.ts", "https://cdn/high/seg1.ts"]
    assert not pl.is_fragmented and pl.init_url is None
    assert all(s.key is None for s in pl.segments)


def test_media_playlist_vazia_falha():
    with pytest.raises(DownloadError):
        hls.parse_media_playlist("#EXTM3U\n#EXT-X-ENDLIST\n", "https://cdn/x")


def test_media_playlist_com_chave_e_iv_explicito():
    text = (
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:5\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="https://cdn/key1",IV=0x000102030405060708090a0b0c0d0e0f\n'
        "seg5.ts\nseg6.ts\n"
        "#EXT-X-KEY:METHOD=NONE\nseg7.ts\n"
    )
    pl = hls.parse_media_playlist(text, "https://cdn/")
    assert pl.segments[0].sequence == 5 and pl.segments[1].sequence == 6
    assert pl.segments[0].key.uri == "https://cdn/key1" and pl.segments[0].key.iv == bytes(range(16))
    assert pl.segments[1].key == pl.segments[0].key  # mesma chave até o próximo EXT-X-KEY
    assert pl.segments[2].key is None  # METHOD=NONE desliga a criptografia


def test_iv_sem_explicito_deriva_da_sequencia():
    text = (
        "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:2\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="k"\n'
        "seg2.ts\n"
    )
    pl = hls.parse_media_playlist(text, "https://cdn/")
    seg = pl.segments[0]
    assert seg.key.iv is None
    assert hls.derive_iv(seg.key, seg.sequence) == (2).to_bytes(16, "big")


def test_iv_invalido_levanta():
    text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="k",IV=0xAB\nseg.ts\n'
    with pytest.raises(DownloadError):
        hls.parse_media_playlist(text, "https://cdn/")


def test_fmp4_detectado_por_ext_x_map_e_por_extensao():
    text = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\nseg0.m4s\nseg1.m4s\n'
    pl = hls.parse_media_playlist(text, "https://cdn/")
    assert pl.is_fragmented and pl.init_url == "https://cdn/init.mp4"

    pl2 = hls.parse_media_playlist("#EXTM3U\nseg0.m4s\nseg1.m4s\n", "https://cdn/")
    assert pl2.is_fragmented and pl2.init_url is None


def test_decrypt_aes128_round_trip_com_cryptography():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as _padding

    key, iv, plaintext = os.urandom(16), os.urandom(16), b"conteudo de video de teste " * 10
    padder = _padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    ciphertext = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(padded)
    assert hls.decrypt_segment(ciphertext, key, iv) == plaintext


def test_decrypt_metodo_nao_suportado():
    key = hls.Key("SAMPLE-AES", "k")
    with pytest.raises(DownloadError):
        run(hls.download_hls(*_client_for_encrypted(key)[:2], "/tmp/x.ts"))


def _client_for_encrypted(key, plaintext=b"x" * 20):
    from fakes import jresp

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    text = f'#EXTM3U\n#EXT-X-KEY:METHOD={key.method},URI="https://cdn/key"\nseg0.ts\n'
    backend.files["https://cdn/media.m3u8"] = text.encode()
    backend.files["https://cdn/key"] = os.urandom(16)
    backend.files["https://cdn/seg0.ts"] = plaintext
    return client, backend, "https://cdn/media.m3u8"


def test_download_hls_plano_sem_criptografia(tmp_path):
    from fakes import jresp

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    backend.files["https://cdn/media.m3u8"] = MEDIA_PLAIN.encode()
    backend.files["https://cdn/seg0.ts"] = b"AAAA"
    backend.files["https://cdn/seg1.ts"] = b"BBBB"
    dest = str(tmp_path / "v.ts")
    updates = []
    bar = type("B", (), {"update": lambda self, n=1: updates.append(n)})()
    stats = run(hls.download_hls(client, "https://cdn/media.m3u8", dest, bar=bar))
    assert stats == {"segments": 2, "bytes": 8, "fragmented": False}
    assert open(dest, "rb").read() == b"AAAABBBB" and updates == [1, 1]


def test_download_hls_com_master_seleciona_variante(tmp_path):
    from fakes import jresp

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    backend.files["https://cdn/master.m3u8"] = MASTER.encode()
    backend.files["https://cdn/high/index.m3u8"] = MEDIA_PLAIN.encode()
    backend.files["https://cdn/high/seg0.ts"] = b"11"
    backend.files["https://cdn/high/seg1.ts"] = b"22"
    dest = str(tmp_path / "v.ts")
    stats = run(hls.download_hls(client, "https://cdn/master.m3u8", dest, quality="HIGH"))
    assert stats["bytes"] == 4 and open(dest, "rb").read() == b"1122"


def test_download_hls_decripta_segmentos(tmp_path):
    from fakes import jresp
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as _padding

    key_bytes, plaintexts = os.urandom(16), [b"seg-A-conteudo", b"seg-B-conteudo-2"]

    def enc(pt, iv):
        padder = _padding.PKCS7(128).padder()
        padded = padder.update(pt) + padder.finalize()
        return Cipher(algorithms.AES(key_bytes), modes.CBC(iv)).encryptor().update(padded)

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    media = (
        '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n'
        '#EXT-X-KEY:METHOD=AES-128,URI="https://cdn/key"\n'
        "seg0.ts\nseg1.ts\n"
    )
    backend.files["https://cdn/media.m3u8"] = media.encode()
    backend.files["https://cdn/key"] = key_bytes
    backend.files["https://cdn/seg0.ts"] = enc(plaintexts[0], (0).to_bytes(16, "big"))
    backend.files["https://cdn/seg1.ts"] = enc(plaintexts[1], (1).to_bytes(16, "big"))
    dest = str(tmp_path / "v.ts")
    stats = run(hls.download_hls(client, "https://cdn/media.m3u8", dest))
    assert open(dest, "rb").read() == b"".join(plaintexts)
    assert stats["segments"] == 2


def test_download_hls_fmp4_concatena_init_e_segmentos(tmp_path):
    from fakes import jresp

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    media = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\nseg0.m4s\nseg1.m4s\n'
    backend.files["https://cdn/media.m3u8"] = media.encode()
    backend.files["https://cdn/init.mp4"] = b"INIT"
    backend.files["https://cdn/seg0.m4s"] = b"F0"
    backend.files["https://cdn/seg1.m4s"] = b"F1"
    dest = str(tmp_path / "v.mp4")
    stats = run(hls.download_hls(client, "https://cdn/media.m3u8", dest))
    assert stats["fragmented"] is True
    assert open(dest, "rb").read() == b"INITF0F1"


def test_download_hls_segmento_ausente_e_erro_permanente(tmp_path):
    from fakes import jresp

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    backend.files["https://cdn/media.m3u8"] = MEDIA_PLAIN.encode()
    # seg0 existe, seg1 não (404 real via handler)
    backend.files["https://cdn/seg0.ts"] = b"X"
    with pytest.raises(PermanentDownloadError):
        run(hls.download_hls(client, "https://cdn/media.m3u8", str(tmp_path / "v.ts")))


def test_download_hls_retry_em_falha_de_rede(tmp_path):
    from fakes import jresp

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    backend.files["https://cdn/media.m3u8"] = MEDIA_PLAIN.encode()
    backend.files["https://cdn/seg0.ts"] = b"AAAA"
    backend.files["https://cdn/seg1.ts"] = b"BBBB"
    backend.cut["https://cdn/seg0.ts"] = 0  # cai uma vez, sem soltar nenhum byte
    avisos = []
    stats = run(hls.download_hls(
        client, "https://cdn/media.m3u8", str(tmp_path / "v.ts"),
        retries=3, on_retry=lambda n, exc: avisos.append(n),
    ))
    assert stats["bytes"] == 8 and len(avisos) == 1


def test_download_hls_falta_cryptography_da_mensagem_clara(tmp_path, monkeypatch):
    import builtins
    from fakes import jresp

    real_import = builtins.__import__

    def blocked(name, *a, **k):
        if name.startswith("cryptography"):
            raise ImportError("bloqueado no teste")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", blocked)

    def handler(method, url, params, data, headers):
        return jresp({}, 404)

    client, backend = make_client(handler)
    media = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://cdn/key"\nseg0.ts\n'
    backend.files["https://cdn/media.m3u8"] = media.encode()
    backend.files["https://cdn/key"] = os.urandom(16)
    backend.files["https://cdn/seg0.ts"] = os.urandom(32)
    with pytest.raises(hls.MissingDependencyError, match="cryptography"):
        run(hls.download_hls(client, "https://cdn/media.m3u8", str(tmp_path / "v.ts"), retries=2))
