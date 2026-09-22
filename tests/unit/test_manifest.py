"""Testes do manifesto (BTS/DASH), proteção e fallback de qualidade."""

import asyncio
import json

import pytest

from fakes import b64, dash_mpd
from tidal_dl import manifest as m
from tidal_dl.exceptions import ForbiddenError, InvalidQuality, NonStreamable, PreviewOnly, UnsupportedProtection


def run(c):
    return asyncio.run(c)


def bts(urls=("http://a/1.flac",), codecs="flac", enc="NONE"):
    return {"manifestMimeType": "application/vnd.tidal.bts", "audioQuality": "LOSSLESS",
            "assetPresentation": "FULL",
            "manifest": b64(json.dumps({"urls": list(urls), "codecs": codecs, "encryptionType": enc}))}


def dash(enc="NONE", segs=3, quality="HI_RES_LOSSLESS"):
    return {"manifestMimeType": "application/dash+xml", "audioQuality": quality, "assetPresentation": "FULL",
            "encryptionType": enc, "manifest": b64(dash_mpd("https://cdn/t", segs)),
            "bitDepth": 24, "sampleRate": 96000, "trackReplayGain": -6.5, "albumReplayGain": -7.0}


def test_parse_dash_expande_template():
    codec, urls = m.parse_dash(dash_mpd("https://cdn/t", 3))
    assert codec == "flac"
    assert urls == ["https://cdn/t/init.mp4", "https://cdn/t/seg1.m4s", "https://cdn/t/seg2.m4s", "https://cdn/t/seg3.m4s"]


@pytest.mark.parametrize("xml", ["<nao xml", "<MPD xmlns='urn:mpeg:dash:schema:mpd:2011'/>"])
def test_parse_dash_invalido(xml):
    with pytest.raises(NonStreamable):
        m.parse_dash(xml)


def test_stream_dash_ok_com_replaygain():
    s = m.stream_from_playback(dash(), 5)
    assert s.is_dash and s.is_flac and s.extension == "flac" and len(s.urls) == 4
    assert (s.bit_depth, s.sample_rate, s.replay_gain, s.album_replay_gain) == (24, 96000, -6.5, -7.0)


def test_stream_bts_ok_e_aac():
    s = m.stream_from_playback(bts(), 5)
    assert not s.is_dash and s.urls == ["http://a/1.flac"]
    aac = m.stream_from_playback(bts(codecs="mp4a.40.2"), 5)
    assert aac.extension == "m4a" and not aac.is_flac


@pytest.mark.parametrize("info", [dash(enc="AES"), bts(enc="OLD_AES")])
def test_protegido_levanta(info):
    with pytest.raises(UnsupportedProtection):
        m.stream_from_playback(info, 5)


@pytest.mark.parametrize("enc", ["NONE", "", "OFFLINE_ONLY", "OfflineOnly"])
def test_encriptacao_aberta_passa(enc):
    assert m.stream_from_playback(dash(enc=enc), 1).is_dash


def test_preview_e_manifest_ruim():
    info = bts()
    info["assetPresentation"] = "PREVIEW"
    with pytest.raises(PreviewOnly):
        m.stream_from_playback(info, 1)
    with pytest.raises(NonStreamable):
        m.stream_from_playback({"manifestMimeType": "x", "manifest": "@@@"}, 1)
    with pytest.raises(NonStreamable):
        m.stream_from_playback(bts(urls=()), 1)


class FakeAPI:
    def __init__(self, plan):
        self.plan, self.asked = plan, []

    async def playback_info(self, track_id, quality):
        self.asked.append(quality)
        r = self.plan[quality]
        if isinstance(r, Exception):
            raise r
        return r


def test_resolve_desce_de_qualidade_em_403_e_protegido():
    api = FakeAPI({"HI_RES_LOSSLESS": ForbiddenError("sem plano"), "HI_RES": dash(enc="AES"),
                   "LOSSLESS": bts()})
    fallbacks = []
    s = run(m.resolve_stream(api, 1, 4, on_fallback=lambda a, b, e: fallbacks.append((a, b))))
    assert api.asked == ["HI_RES_LOSSLESS", "HI_RES", "LOSSLESS"] and s.quality == "LOSSLESS"
    assert fallbacks == [("HI_RES_LOSSLESS", "HI_RES"), ("HI_RES", "LOSSLESS")]


def test_resolve_sem_fallback_falha_na_primeira():
    api = FakeAPI({"HI_RES_LOSSLESS": NonStreamable("região")})
    with pytest.raises(NonStreamable):
        run(m.resolve_stream(api, 1, 4, allow_fallback=False))
    assert api.asked == ["HI_RES_LOSSLESS"]


def test_resolve_tudo_indisponivel():
    api = FakeAPI({q: NonStreamable("x") for q in ("LOW", "HIGH", "LOSSLESS")})
    with pytest.raises(NonStreamable):
        run(m.resolve_stream(api, 1, 2))
    assert api.asked == ["LOSSLESS", "HIGH", "LOW"]


def test_resolve_preview_nao_desce_e_qualidade_invalida():
    info = bts()
    info["assetPresentation"] = "PREVIEW"
    api = FakeAPI({"LOSSLESS": info})
    with pytest.raises(PreviewOnly):
        run(m.resolve_stream(api, 1, 2))
    assert api.asked == ["LOSSLESS"]
    with pytest.raises(InvalidQuality):
        run(m.resolve_stream(api, 1, 9))


def test_resolve_403_em_todas_vira_nonstreamable():
    api = FakeAPI({q: ForbiddenError("x") for q in ("LOW", "HIGH")})
    with pytest.raises(NonStreamable):
        run(m.resolve_stream(api, 1, 1))
