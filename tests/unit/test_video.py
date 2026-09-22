"""Testes de download de vídeos do Tidal."""

import asyncio
import os
import pytest

from fakes import Response, jresp, make_client
from tidal_dl.api import TidalAPI
from tidal_dl.auth import Credentials
from tidal_dl.downloader import Downloader
from tidal_dl.manifest import resolve_video_stream
from tidal_dl.models import Video
from tidal_dl.settings import TidalDLSettings


def test_video_model():
    data = {
        "id": 12345,
        "title": "Music Video",
        "artist": {"name": "Artist Name"},
        "duration": 210,
        "releaseDate": "2023-05-01T00:00:00.000+0000",
    }
    v = Video.from_dict(data)
    assert v.id == 12345
    assert v.full_title == "Music Video"
    assert v.artist_names == "Artist Name"
    assert v.release_date == "2023-05-01"


def test_resolve_video_stream():
    manifest_json = '{"urls": ["https://video.tidal.com/stream.m3u8"]}'
    import base64
    b64 = base64.b64encode(manifest_json.encode()).decode()

    def handler(method, url, params, data, headers):
        if "videos/12345/playbackinfopostpaywall" in url:
            return jresp({
                "assetPresentation": "FULL",
                "manifestMimeType": "application/vnd.tidal.emu",
                "manifest": b64
            })
        return jresp({})

    client, backend = make_client(handler)
    api = TidalAPI(client, Credentials("t", "r"))
    stream = asyncio.run(resolve_video_stream(api, 12345, "1080p"))
    assert stream.track_id == 12345
    assert stream.is_m3u8 is True
    assert stream.urls == ["https://video.tidal.com/stream.m3u8"]


def test_download_video_flow(tmp_path):
    manifest_json = '{"urls": ["https://video.tidal.com/segment1.ts"]}'
    import base64
    b64 = base64.b64encode(manifest_json.encode()).decode()

    def handler(method, url, params, data, headers):
        if "videos/12345/playbackinfopostpaywall" in url:
            return jresp({
                "assetPresentation": "FULL",
                "manifestMimeType": "application/vnd.tidal.emu",
                "manifest": b64
            })
        if "videos/12345" in url:
            return jresp({
                "id": 12345,
                "title": "Test Video",
                "artist": {"name": "Test Artist"}
            })
        return jresp({})

    client, backend = make_client(handler)
    backend.files["https://video.tidal.com/segment1.ts"] = b"VIDEO_TS_DATA_123"

    api = TidalAPI(client, Credentials("t", "r"))
    settings = TidalDLSettings(video_directory=str(tmp_path / "Videos"))
    dl = Downloader(api, settings)

    res = asyncio.run(dl.download_video(12345))
    assert res.ok is True
    assert len(res.tracks) == 1
    assert res.tracks[0].success is True
    assert os.path.exists(res.tracks[0].path)
    with open(res.tracks[0].path, "rb") as fh:
        assert fh.read() == b"VIDEO_TS_DATA_123"
