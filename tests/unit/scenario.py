"""Cenário completo de Tidal falso (álbum + faixas + DASH/BTS + letras + capa)."""

from __future__ import annotations

import json

import fakes
from tidal_dl import auth
from tidal_dl.api import TidalAPI
from tidal_dl.settings import TidalDLSettings

COVER = "https://resources.tidal.com/images/aa/bb/cc/1280x1280.jpg"


class Scenario:
    def __init__(self, tmp_path, *, tracks=2, volumes=1, album_id=10, quality="HI_RES_LOSSLESS", **settings):
        self.album_id, self.n, self.volumes, self.quality = album_id, tracks, volumes, quality
        self.protected: set[tuple[int, str]] = set()  # (faixa, tier) que vêm criptografados
        self.forbidden: set[tuple[int, str]] = set()
        self.status401_tracks: set[int] = set()
        self.playlist = None
        self.asked: list[tuple[int, str]] = []
        self.fail_once: set[str] = set()
        self.client, self.backend = fakes.make_client(self.handler)
        orig_stream = self.backend.stream

        import contextlib

        @contextlib.asynccontextmanager
        async def stream(url, *, headers=None, timeout=None):
            if url in self.fail_once:
                self.fail_once.discard(url)
                yield fakes.FakeStream(503, b"")
                return
            async with orig_stream(url, headers=headers, timeout=timeout) as s:
                yield s

        self.backend.stream = stream
        for t in range(1, tracks + 1):
            self.add_track_files(t)
        self.backend.files[COVER] = b"\xff\xd8\xff" + b"c" * 40
        self.creds = auth.Credentials("tok", "ref", 9e12, "7", "BR")
        self.api = TidalAPI(self.client, self.creds)
        self.dir = str(tmp_path / "music")
        self.settings = TidalDLSettings(directory=self.dir, **settings)
        self.settings.validate()
        self.db = str(tmp_path / "t.db")

    # -- dados ---------------------------------------------------------
    def track_json(self, t: int, album=None):
        vol = 1 if self.volumes == 1 else 1 + (t - 1) * self.volumes // self.n
        return {"id": t, "title": f"Song{t}", "trackNumber": t if self.volumes == 1 else 1, "volumeNumber": vol,
                "artist": {"id": 1, "name": "Art"}, "artists": [{"id": 1, "name": "Art", "type": "MAIN"}],
                "album": {"id": album or self.album_id, "title": "Alb", "cover": "aa-bb-cc"},
                "isrc": f"I{t}", "replayGain": -5.0, "explicit": t == 2}

    def add_track_files(self, t: int):
        for tier in ("dash", "bts"):
            pass
        self.backend.files[f"https://cdn/{t}/init.mp4"] = fakes.make_init(96000, 2, 24)
        for n in (1, 2):
            self.backend.files[f"https://cdn/{t}/seg{n}.m4s"] = fakes.make_segment(b"F%d" % t * 300)
        self.backend.files[f"https://cdn/{t}/plain.flac"] = b"fLaC" + b"P" * 200

    def playback(self, tid: int, tier: str):
        if (tid, tier) in self.protected:
            return {"manifestMimeType": "application/dash+xml", "audioQuality": tier, "assetPresentation": "FULL",
                    "encryptionType": "AES", "manifest": fakes.b64(fakes.dash_mpd(f"https://cdn/{tid}", 2))}
        if tier == "HI_RES_LOSSLESS":
            return {"manifestMimeType": "application/dash+xml", "audioQuality": tier, "assetPresentation": "FULL",
                    "encryptionType": "NONE", "manifest": fakes.b64(fakes.dash_mpd(f"https://cdn/{tid}", 2))}
        body = {"urls": [f"https://cdn/{tid}/plain.flac"], "codecs": "flac" if tier != "HIGH" else "mp4a.40.2",
                "encryptionType": "NONE"}
        return {"manifestMimeType": "application/vnd.tidal.bts", "audioQuality": tier, "assetPresentation": "FULL",
                "manifest": fakes.b64(json.dumps(body))}

    def handler(self, method, url, params, data, headers):
        if "oauth2/token" in url:
            return fakes.jresp({"access_token": "novo", "expires_in": 100})
        if "/albums/" in url and url.endswith("/items"):
            ids = range(1, self.n + 1)
            return fakes.jresp({"items": [{"type": "track", "item": self.track_json(t)} for t in ids],
                                "totalNumberOfItems": self.n})
        if "/albums/" in url:
            return fakes.jresp({"id": self.album_id, "title": "Alb", "artist": {"id": 1, "name": "Art"},
                                "artists": [{"id": 1, "name": "Art", "type": "MAIN"}], "releaseDate": "2020-01-02",
                                "numberOfTracks": self.n, "numberOfVolumes": self.volumes, "cover": "aa-bb-cc",
                                "audioQuality": self.quality, "upc": "999", "type": "ALBUM"})
        if "/playlists/" in url and url.endswith("/items"):
            return fakes.jresp({"items": [{"type": "track", "item": self.track_json(t)} for t in range(1, self.n + 1)],
                                "totalNumberOfItems": self.n})
        if "/playlists/" in url:
            return fakes.jresp({"uuid": "pl-1", "title": "Minha Lista", "numberOfTracks": self.n,
                                "creator": {"name": "Eu"}})
        if "playbackinfopostpaywall" in url:
            tid = int(url.split("/tracks/")[1].split("/")[0])
            tier = params["audioquality"]
            self.asked.append((tid, tier))
            if tid in self.status401_tracks:
                return fakes.jresp({"userMessage": "token"}, 401)
            if (tid, tier) in self.forbidden:
                return fakes.jresp({"userMessage": "forbidden"}, 403)
            return fakes.jresp(self.playback(tid, tier))
        if "/search/" in url:
            album = {"id": self.album_id, "title": "Alb", "artist": {"id": 1, "name": "Art"},
                     "releaseDate": "2020-01-02", "audioQuality": self.quality}
            return fakes.jresp({"items": [album], "totalNumberOfItems": 1, "limit": 20, "offset": 0})
        if url.endswith("/artists/5/albums"):
            return fakes.jresp({"items": [{"id": self.album_id, "title": "Alb", "releaseDate": "2020-01-02"}],
                                "totalNumberOfItems": 1, "limit": 100, "offset": 0})
        if url.endswith("/artists/5"):
            return fakes.jresp({"id": 5, "name": "Art"})
        if url.endswith("/lyrics"):
            return fakes.jresp({"lyrics": "la la", "subtitles": "[00:01.00] la la"})
        if "/tracks/" in url:
            tid = int(url.rsplit("/", 1)[1])
            return fakes.jresp(self.track_json(tid))
        return fakes.jresp({"userMessage": "?"}, 404)

    def downloader(self, **kw):
        from tidal_dl.downloader import Downloader

        return Downloader(self.api, self.settings, db_path=self.db, **kw)
