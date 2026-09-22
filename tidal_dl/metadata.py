"""Tags de áudio (FLAC/M4A) e capa.

A montagem dos tags (``build_tags``) é PURA e testável; a gravação usa
``mutagen`` com import tardio. O que é gravado espelha o qobuz-dl-ultra
(ISRC, BARCODE, ReplayGain, IDs do serviço para o ``scan`` casar por tag).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from tidal_dl.models import Album, Stream, Track

logger = logging.getLogger(__name__)


def image_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def _db(v: Optional[float]) -> Optional[str]:
    return f"{v:.2f} dB" if v is not None else None


def _peak(v: Optional[float]) -> Optional[str]:
    return f"{v:.6f}" if v is not None else None


def build_tags(
    track: Track,
    album: Album,
    stream: Stream,
    *,
    total_tracks: Optional[int] = None,
    total_discs: Optional[int] = None,
    lyrics: str = "",
) -> dict[str, str]:
    """Dicionário ``CHAVE -> valor`` (estilo Vorbis). Valores vazios são omitidos."""
    tg = stream.replay_gain if stream.replay_gain is not None else track.replay_gain
    tp = stream.peak if stream.peak is not None else track.peak
    tags: dict[str, Any] = {
        "TITLE": track.full_title,
        "ARTIST": track.artist_names,
        "ALBUMARTIST": album.album_artist,
        "ALBUM": album.full_title,
        "TRACKNUMBER": track.track_number or None,
        "TRACKTOTAL": total_tracks or album.number_of_tracks or None,
        "DISCNUMBER": track.volume_number or None,
        "DISCTOTAL": total_discs or album.number_of_volumes or None,
        "DATE": album.release_date or None,
        "YEAR": album.year if album.release_date else None,
        "ISRC": track.isrc or None,
        "BARCODE": album.upc or None,
        "COPYRIGHT": track.copyright or album.copyright or None,
        "BPM": track.bpm,
        "LYRICS": lyrics or None,
        "REPLAYGAIN_TRACK_GAIN": _db(tg),
        "REPLAYGAIN_TRACK_PEAK": _peak(tp),
        "REPLAYGAIN_ALBUM_GAIN": _db(stream.album_replay_gain),
        "REPLAYGAIN_ALBUM_PEAK": _peak(stream.album_peak),
        "TIDALTRACKID": track.id or None,
        "TIDALALBUMID": album.id or None,
        "ITUNESADVISORY": "1" if track.explicit else None,
    }
    return {k: str(v) for k, v in tags.items() if v not in (None, "", 0)}


def tag_flac(path: str, tags: dict[str, str], cover: Optional[bytes] = None) -> None:
    from mutagen.flac import FLAC, Picture  # noqa: PLC0415

    audio = FLAC(path)
    audio.clear()
    for key, value in tags.items():
        audio[key] = [value]
    if cover:
        pic = Picture()
        pic.type = 3  # capa frontal
        pic.mime = image_mime(cover)
        pic.data = cover
        audio.clear_pictures()
        audio.add_picture(pic)
    audio.save()


def _freeform(value: str) -> list:
    from mutagen.mp4 import MP4FreeForm  # noqa: PLC0415

    return [MP4FreeForm(value.encode("utf-8"))]


def tag_m4a(path: str, tags: dict[str, str], cover: Optional[bytes] = None) -> None:
    from mutagen.mp4 import MP4, MP4Cover  # noqa: PLC0415

    audio = MP4(path)
    audio.delete()
    audio["\xa9nam"] = [tags.get("TITLE", "")]
    audio["\xa9ART"] = [tags.get("ARTIST", "")]
    audio["aART"] = [tags.get("ALBUMARTIST", "")]
    audio["\xa9alb"] = [tags.get("ALBUM", "")]
    audio["trkn"] = [(int(tags.get("TRACKNUMBER", 0) or 0), int(tags.get("TRACKTOTAL", 0) or 0))]
    audio["disk"] = [(int(tags.get("DISCNUMBER", 1) or 1), int(tags.get("DISCTOTAL", 1) or 1))]
    if tags.get("DATE"):
        audio["\xa9day"] = [tags["DATE"]]
    if tags.get("COPYRIGHT"):
        audio["cprt"] = [tags["COPYRIGHT"]]
    if tags.get("LYRICS"):
        audio["\xa9lyr"] = [tags["LYRICS"]]
    if tags.get("BPM"):
        audio["tmpo"] = [int(tags["BPM"])]
    if tags.get("ITUNESADVISORY"):
        audio["rtng"] = [1]
    for key in ("ISRC", "BARCODE", "TIDALTRACKID", "TIDALALBUMID",
                "REPLAYGAIN_TRACK_GAIN", "REPLAYGAIN_TRACK_PEAK",
                "REPLAYGAIN_ALBUM_GAIN", "REPLAYGAIN_ALBUM_PEAK"):
        if tags.get(key):
            audio[f"----:com.apple.iTunes:{key}"] = _freeform(tags[key])
    if cover:
        fmt = MP4Cover.FORMAT_PNG if image_mime(cover) == "image/png" else MP4Cover.FORMAT_JPEG
        audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
    audio.save()


def tag_file(path: str, tags: dict[str, str], cover: Optional[bytes] = None) -> None:
    """Grava tags conforme a extensão. Levanta em erro (quem chama decide)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        tag_flac(path, tags, cover)
    elif ext in (".m4a", ".mp4"):
        tag_m4a(path, tags, cover)
    else:
        raise ValueError(f"extensão sem suporte a tags: {ext}")
