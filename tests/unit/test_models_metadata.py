"""Testes de modelos, tags e letras."""

from tidal_dl import lyrics as ly
from tidal_dl import metadata as md
from tidal_dl.models import Album, Page, Stream, Track, join_artists, Artist, with_version


def _album(**kw):
    d = {"id": 1, "title": "X", "artist": {"name": "A"}, "releaseDate": "2020-05-01", "numberOfTracks": 10, "upc": "123"}
    d.update(kw)
    return Album.from_dict(d)


def test_with_version():
    assert with_version("Song", "Live") == "Song (Live)"
    assert with_version("Song (Live)", "live") == "Song (Live)"
    assert with_version("Song", None) == "Song"


def test_album_derivados():
    a = _album(version="Deluxe", type="EP", artists=[{"name": "A", "type": "MAIN"}, {"name": "B", "type": "FEATURED"}],
               audioQuality="HI_RES_LOSSLESS")
    assert (a.full_title, a.album_artist, a.year, a.release_type, a.max_quality_rank) == ("X (Deluxe)", "A", "2020", "EP", 4)
    assert _album(type="SINGLE").release_type == "Single" and _album(type="ALBUM").release_type == "Album"
    assert _album(releaseDate="").year == "0000"


def test_modelos_toleram_campos_ausentes():
    assert Track.from_dict({}).title == "Unknown" and Album.from_dict({}).number_of_volumes == 1
    t = Track.from_dict({"id": "5", "title": "S", "trackNumber": None, "streamReady": False})
    assert t.id == 5 and t.track_number == 0 and not t.available
    assert join_artists([Artist(1, "A"), Artist(2, "A")]) == "A" and join_artists([]) == "Unknown"


def test_page():
    p = Page.from_dict({"items": [{}, {}], "totalNumberOfItems": 5, "limit": 2, "offset": 0})
    assert p.has_more and not Page.from_dict({}).has_more


def test_stream_extensoes():
    assert Stream(1, "LOSSLESS", "flac", ["u"]).extension == "flac"
    assert Stream(1, "HIGH", "mp4a.40.2", ["u"]).extension == "m4a"


def test_build_tags_completo():
    t = Track.from_dict({"id": 5, "title": "S", "version": "Live", "artist": {"name": "A"}, "trackNumber": 3,
                         "explicit": True, "isrc": "I1", "replayGain": -6.5, "peak": 0.98, "bpm": 120})
    s = Stream(5, "LOSSLESS", "flac", ["u"], album_replay_gain=-7.0, album_peak=0.99)
    tags = md.build_tags(t, _album(), s, lyrics="la")
    assert tags["TITLE"] == "S (Live)" and tags["TRACKNUMBER"] == "3" and tags["TRACKTOTAL"] == "10"
    assert tags["REPLAYGAIN_TRACK_GAIN"] == "-6.50 dB" and tags["REPLAYGAIN_ALBUM_PEAK"] == "0.990000"
    assert tags["TIDALTRACKID"] == "5" and tags["TIDALALBUMID"] == "1" and tags["BARCODE"] == "123"
    assert tags["ITUNESADVISORY"] == "1" and tags["BPM"] == "120" and tags["LYRICS"] == "la" and tags["YEAR"] == "2020"


def test_build_tags_omite_vazios_e_prefere_gain_do_stream():
    t = Track.from_dict({"id": 5, "title": "S", "artist": {"name": "A"}, "replayGain": -1.0})
    s = Stream(5, "LOSSLESS", "flac", ["u"], replay_gain=-9.0)
    tags = md.build_tags(t, _album(releaseDate="", upc=""), s)
    assert tags["REPLAYGAIN_TRACK_GAIN"] == "-9.00 dB"
    for k in ("DATE", "YEAR", "ISRC", "BARCODE", "LYRICS", "ITUNESADVISORY", "REPLAYGAIN_ALBUM_GAIN"):
        assert k not in tags


def test_image_mime():
    assert md.image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"
    assert md.image_mime(b"\x89PNG\r\n\x1a\nxx") == "image/png"
    assert md.image_mime(b"???") == "image/jpeg"


def test_lyrics():
    lrc = "[00:01.00] oi\n[00:02.50] tchau"
    assert ly.synced_lyrics({"subtitles": lrc}) == lrc
    assert ly.synced_lyrics({"lyrics": "x"}) is None and ly.synced_lyrics({"subtitles": "texto"}) is None
    assert ly.plain_lyrics({"lyrics": "abc"}) == "abc"
    assert ly.plain_lyrics({"subtitles": lrc}) == "oi\ntchau"
    assert ly.plain_lyrics({}) == ""
