"""Testes de download de vídeo (HLS) via ``Downloader.download_video``."""

import asyncio
import os

import pytest

from fakes import b64, jresp, make_client
from tidal_dl import auth, db, sentinel
from tidal_dl.api import TidalAPI
from tidal_dl.downloader import Downloader
from tidal_dl.exceptions import ResourceNotFoundError
from tidal_dl.settings import TidalDLSettings

MEDIA = "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\nseg0.ts\nseg1.ts\n#EXT-X-ENDLIST\n"


def run(c):
    return asyncio.run(c)


def make(tmp_path, *, video_id=99, handler=None, **kw):
    def default_handler(method, url, params, data, headers):
        if f"videos/{video_id}/playbackinfopostpaywall" in url:
            return jresp({
                "assetPresentation": "FULL", "manifestMimeType": "application/vnd.tidal.emu",
                "manifest": b64('{"urls": ["https://cdn/media.m3u8"]}'),
            })
        if f"videos/{video_id}" in url:
            return jresp({"id": video_id, "title": "Music Video", "artist": {"name": "Artist"},
                          "releaseDate": "2023-05-01"})
        return jresp({}, 404)

    client, backend = make_client(handler or default_handler)
    backend.files["https://cdn/media.m3u8"] = MEDIA.encode()
    backend.files["https://cdn/seg0.ts"] = b"AAAA"
    backend.files["https://cdn/seg1.ts"] = b"BBBB"
    api = TidalAPI(client, auth.Credentials("t", "r", 9e12, "7", "BR"))
    settings = TidalDLSettings(video_directory=str(tmp_path / "Videos"), **kw)
    settings.validate()
    return Downloader(api, settings, db_path=str(tmp_path / "t.db")), backend


def test_download_video_completo(tmp_path, capsys):
    dl, backend = make(tmp_path)
    res = run(dl.download_video(99))
    assert res.ok and res.tracks[0].success
    path = res.tracks[0].path
    assert path.endswith(".ts") and open(path, "rb").read() == b"AAAABBBB"
    out = capsys.readouterr().out
    assert "VÍDEO" in out and "Em Progresso: Artist - Music Video" in out
    assert "└─ Concluído: Artist - Music Video" in out and "RESUMO DA VÍDEO" in out
    assert "[vídeo 01/01] Music Video" in out
    assert os.path.isfile(path + ".json")  # TS não suporta tags nativas
    assert db.is_downloaded(str(tmp_path / "t.db"), 99, "video") == path
    # sentinela é só para álbuns; vídeo não deve criar uma
    assert not sentinel.has_sentinel(os.path.dirname(path))


def test_download_video_pula_se_ja_baixado(tmp_path):
    dl, backend = make(tmp_path)
    res1 = run(dl.download_video(99))
    dl2, _ = make(tmp_path)
    dl2.db_path = dl.db_path
    res2 = run(dl2.download_video(99))
    assert res2.skipped and res2.tracks[0].path == res1.tracks[0].path


def test_download_video_nome_de_arquivo_usa_artista_titulo_ano(tmp_path):
    dl, _ = make(tmp_path)
    res = run(dl.download_video(99))
    assert os.path.basename(res.tracks[0].path) == "Artist - Music Video (2023).ts"


def test_download_video_inexistente(tmp_path):
    def handler(method, url, params, data, headers):
        return jresp({"userMessage": "nao existe"}, 404)

    dl, _ = make(tmp_path, handler=handler)
    with pytest.raises(ResourceNotFoundError):
        run(dl.download_video(99))


def test_download_video_com_ffmpeg_ausente_mantem_ts(tmp_path, monkeypatch):
    from tidal_dl import utils

    monkeypatch.setattr(utils, "encontrar_binario", lambda name: None)
    dl, _ = make(tmp_path)
    res = run(dl.download_video(99))
    assert res.tracks[0].path.endswith(".ts") and res.tracks[0].file_format == "TS"


def test_download_video_remux_none_mantem_ts(tmp_path):
    dl, _ = make(tmp_path, remux="none")
    res = run(dl.download_video(99))
    assert res.tracks[0].path.endswith(".ts")


def test_download_video_falha_de_stream_registra_erro(tmp_path):
    def handler(method, url, params, data, headers):
        if "playbackinfopostpaywall" in url:
            return jresp({"userMessage": "sem manifest"})
        if "videos/99" in url:
            return jresp({"id": 99, "title": "X", "artist": {"name": "A"}})
        return jresp({}, 404)

    dl, _ = make(tmp_path, handler=handler)
    res = run(dl.download_video(99))
    assert not res.ok and "manifest" in res.tracks[0].error.lower()
    assert not any(f.startswith("~tmp_") for f in os.listdir(dl.settings.video_directory))


def test_download_video_com_master_playlist_e_qualidade(tmp_path):
    master = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000\nlow.m3u8\n"
              "#EXT-X-STREAM-INF:BANDWIDTH=5000000\nhigh.m3u8\n")

    def handler(method, url, params, data, headers):
        if "playbackinfopostpaywall" in url:
            assert params["videoquality"] == "LOW"
            return jresp({"assetPresentation": "FULL", "manifestMimeType": "application/vnd.tidal.emu",
                          "manifest": b64('{"urls": ["https://cdn/master.m3u8"]}')})
        if "videos/99" in url:
            return jresp({"id": 99, "title": "X", "artist": {"name": "A"}})
        return jresp({}, 404)

    dl, backend = make(tmp_path, handler=handler, video_quality="low")
    backend.files["https://cdn/master.m3u8"] = master.encode()
    backend.files["https://cdn/low.m3u8"] = MEDIA.encode()
    res = run(dl.download_video(99))
    assert res.ok


def test_download_video_segmentos_criptografados(tmp_path):
    import os as _os
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as _padding

    key = _os.urandom(16)

    def enc(pt, seq):
        iv = seq.to_bytes(16, "big")
        padder = _padding.PKCS7(128).padder()
        padded = padder.update(pt) + padder.finalize()
        return Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor().update(padded)

    def handler(method, url, params, data, headers):
        if "playbackinfopostpaywall" in url:
            return jresp({"assetPresentation": "FULL", "manifestMimeType": "application/vnd.tidal.emu",
                          "manifest": b64('{"urls": ["https://cdn/media.m3u8"]}')})
        if "videos/99" in url:
            return jresp({"id": 99, "title": "X", "artist": {"name": "A"}})
        return jresp({}, 404)

    dl, backend = make(tmp_path, handler=handler)
    media = ('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n'
             '#EXT-X-KEY:METHOD=AES-128,URI="https://cdn/key"\nseg0.ts\n')
    backend.files["https://cdn/media.m3u8"] = media.encode()
    backend.files["https://cdn/key"] = key
    backend.files["https://cdn/seg0.ts"] = enc(b"conteudo secreto do segmento", 0)
    res = run(dl.download_video(99))
    assert res.ok and open(res.tracks[0].path, "rb").read() == b"conteudo secreto do segmento"
