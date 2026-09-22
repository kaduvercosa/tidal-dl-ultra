"""Testes do Lyrics Engine (Musixmatch, Tidal API, LRCLIB, Genius)."""

import os
import pytest
import httpx
from tidal_dl.lyrics_engine import LyricsEngine


def test_musixmatch_lyrics_fetch(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        if "token.get" in url:
            return httpx.Response(200, json={
                "message": {
                    "header": {"status_code": 200},
                    "body": {"user_token": "fake_mxm_token"}
                }
            })
        if "macro.subtitles.get" in url:
            return httpx.Response(200, json={
                "message": {
                    "header": {"status_code": 200},
                    "body": {
                        "macro_calls": {
                            "track.subtitles.get": {
                                "message": {
                                    "header": {"status_code": 200},
                                    "body": {
                                        "subtitle_list": [
                                            {"subtitle": {"subtitle_body": "[00:10.00] Test Lyric"}}
                                        ]
                                    }
                                }
                            }
                        }
                    }
                }
            })
        return httpx.Response(404)

    class MockClient:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            return mock_get(url, **kwargs)

    engine = LyricsEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: MockClient())

    lyr = engine.fetch_musixmatch_lyrics("Test Artist", "Test Song")
    assert lyr == "[00:10.00] Test Lyric"


def test_lrclib_lyrics_fetch(monkeypatch):
    def mock_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(200, json={
            "syncedLyrics": "[00:05.00] LRCLIB Synced",
            "plainLyrics": "LRCLIB Plain"
        })

    class MockClient:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            return mock_get(url, **kwargs)

    engine = LyricsEngine()
    monkeypatch.setattr(engine, "_get_session", lambda: MockClient())

    synced, plain = engine.fetch_lrclib_lyrics("Artist", "Song", "Album")
    assert synced == "[00:05.00] LRCLIB Synced"
    assert plain == "LRCLIB Plain"


def test_fetch_and_inject_musixmatch(tmp_path, monkeypatch):
    engine = LyricsEngine()
    monkeypatch.setattr(engine, "fetch_musixmatch_lyrics", lambda a, t: "[00:01.00] MXM Synced")

    audio_file = tmp_path / "song.flac"
    audio_file.write_bytes(b"FLAC_DATA")

    res = engine.fetch_and_inject(str(audio_file), "Artist", "Title", save_lrc=True, embed_lyrics=False)
    assert res["success"] is True
    assert res["source"] == "Musixmatch"
    assert res["synchronized"] is True

    lrc_file = tmp_path / "song.lrc"
    assert lrc_file.exists()
    assert "[by:Musixmatch]" in lrc_file.read_text(encoding="utf-8")
    assert "[00:01.00] MXM Synced" in lrc_file.read_text(encoding="utf-8")
