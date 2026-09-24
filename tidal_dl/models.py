"""Modelos tipados das respostas da API v1 do Tidal.

Tolerantes de propósito: campo ausente/None vira valor neutro, nunca
``KeyError`` -- a API do Tidal muda sem aviso e uma faixa com metadado faltando
não pode derrubar o álbum inteiro.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from tidal_dl.constants import QUALITY_BY_NAME


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def with_version(title: str, version: Optional[str]) -> str:
    """``"Song"`` + ``"Remastered"`` -> ``"Song (Remastered)"`` (sem duplicar)."""
    title = (title or "").strip()
    version = (version or "").strip()
    if version and version.lower() not in title.lower():
        return f"{title} ({version})"
    return title


@dataclass
class Artist:
    id: int = 0
    name: str = "Unknown"
    type: Optional[str] = None

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Artist":
        if not isinstance(d, dict) or not d:
            return cls()
        return cls(_int(d.get("id")), str(d.get("name") or "Unknown"), d.get("type"))


def join_artists(artists: list[Artist], fallback: str = "Unknown") -> str:
    names = [a.name for a in artists if a.name]
    return ", ".join(dict.fromkeys(names)) or fallback


@dataclass
class Album:
    id: int
    title: str
    version: str = ""
    artist: Artist = field(default_factory=Artist)
    artists: list[Artist] = field(default_factory=list)
    cover: Optional[str] = None
    release_date: str = ""
    duration: int = 0
    number_of_tracks: int = 0
    number_of_volumes: int = 1
    explicit: bool = False
    audio_quality: str = ""
    audio_modes: list[str] = field(default_factory=list)
    # Tags de "mediaMetadata" (ex.: LOSSLESS, HIRES_LOSSLESS, DOLBY_ATMOS) --
    # sinal DIFERENTE de audio_quality/audio_modes. audio_quality/audio_modes
    # dizem qual e' o mix "principal"/default retornado pela API; tags dizem
    # quais formatos de entrega EXISTEM pra esse conteudo. Um album pode ter
    # audio_quality="HI_RES_LOSSLESS" (mix estereo e' o principal) e AINDA
    # ASSIM ter um mix Dolby Atmos disponivel só' visivel aqui -- sem checar
    # tags, esse album nunca tentava o tier Atmos (ver is_dolby_atmos() em
    # downloader.py). Mesmo campo que o tiddl usa (Track.MediaMetadata.tags).
    media_metadata_tags: list[str] = field(default_factory=list)
    type: str = "ALBUM"
    upc: str = ""
    copyright: str = ""
    label: str = ""
    url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Album":
        d = d or {}
        media_metadata = d.get("mediaMetadata") if isinstance(d.get("mediaMetadata"), dict) else {}
        return cls(
            id=_int(d.get("id")),
            title=str(d.get("title") or "Unknown"),
            version=str(d.get("version") or ""),
            artist=Artist.from_dict(d.get("artist")),
            artists=[Artist.from_dict(a) for a in (d.get("artists") or [])],
            cover=d.get("cover"),
            release_date=str(d.get("releaseDate") or d.get("streamStartDate") or "")[:10],
            duration=_int(d.get("duration")),
            number_of_tracks=_int(d.get("numberOfTracks")),
            number_of_volumes=max(1, _int(d.get("numberOfVolumes"), 1)),
            explicit=bool(d.get("explicit")),
            audio_quality=str(d.get("audioQuality") or "").upper(),
            audio_modes=[str(m).upper() for m in (d.get("audioModes") or [])],
            media_metadata_tags=[str(t).upper() for t in (media_metadata.get("tags") or [])],
            type=str(d.get("type") or "ALBUM").upper(),
            upc=str(d.get("upc") or ""),
            copyright=str(d.get("copyright") or ""),
            label=str(d.get("label") or ""),
            url=str(d.get("url") or ""),
        )

    @property
    def full_title(self) -> str:
        return with_version(self.title, self.version)

    @property
    def album_artist(self) -> str:
        main = [a for a in self.artists if (a.type or "").upper() == "MAIN"]
        return join_artists(main or self.artists, self.artist.name)

    @property
    def year(self) -> str:
        return self.release_date[:4] or "0000"

    @property
    def release_type(self) -> str:
        return {"EP": "EP", "SINGLE": "Single"}.get(self.type, "Album")

    @property
    def max_quality_rank(self) -> Optional[int]:
        return QUALITY_BY_NAME.get(self.audio_quality)


@dataclass
class Track:
    id: int
    title: str
    version: str = ""
    artist: Artist = field(default_factory=Artist)
    artists: list[Artist] = field(default_factory=list)
    album_id: int = 0
    album_title: str = ""
    album_cover: Optional[str] = None
    track_number: int = 0
    volume_number: int = 1
    duration: int = 0
    explicit: bool = False
    isrc: str = ""
    audio_quality: str = ""
    audio_modes: list[str] = field(default_factory=list)
    # Ver comentário equivalente em Album.media_metadata_tags.
    media_metadata_tags: list[str] = field(default_factory=list)
    copyright: str = ""
    replay_gain: Optional[float] = None
    peak: Optional[float] = None
    stream_ready: bool = True
    allow_streaming: bool = True
    bpm: Optional[int] = None
    url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        d = d or {}
        album = d.get("album") if isinstance(d.get("album"), dict) else {}
        media_metadata = d.get("mediaMetadata") if isinstance(d.get("mediaMetadata"), dict) else {}
        return cls(
            id=_int(d.get("id")),
            title=str(d.get("title") or "Unknown"),
            version=str(d.get("version") or ""),
            artist=Artist.from_dict(d.get("artist")),
            artists=[Artist.from_dict(a) for a in (d.get("artists") or [])],
            album_id=_int(album.get("id")),
            album_title=str(album.get("title") or ""),
            album_cover=album.get("cover"),
            track_number=_int(d.get("trackNumber")),
            volume_number=max(1, _int(d.get("volumeNumber"), 1)),
            duration=_int(d.get("duration")),
            explicit=bool(d.get("explicit")),
            isrc=str(d.get("isrc") or ""),
            audio_quality=str(d.get("audioQuality") or "").upper(),
            audio_modes=[str(m).upper() for m in (d.get("audioModes") or [])],
            media_metadata_tags=[str(t).upper() for t in (media_metadata.get("tags") or [])],
            copyright=str(d.get("copyright") or ""),
            replay_gain=_float(d.get("replayGain")),
            peak=_float(d.get("peak")),
            stream_ready=d.get("streamReady", True) is not False,
            allow_streaming=d.get("allowStreaming", True) is not False,
            bpm=_int(d.get("bpm")) or None,
            url=str(d.get("url") or ""),
        )

    @property
    def full_title(self) -> str:
        return with_version(self.title, self.version)

    @property
    def artist_names(self) -> str:
        return join_artists(self.artists, self.artist.name)

    @property
    def available(self) -> bool:
        return self.stream_ready and self.allow_streaming


@dataclass
class Video:
    """Vídeo musical do Tidal (endpoint ``videos/{id}``)."""

    id: int
    title: str
    version: str = ""
    artist: Artist = field(default_factory=Artist)
    artists: list[Artist] = field(default_factory=list)
    duration: int = 0
    explicit: bool = False
    release_date: str = ""
    quality: str = ""  # "HIGH" | "MEDIUM" | "LOW" (o que a API devolveu como audioQuality/max)
    stream_ready: bool = True
    allow_streaming: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Video":
        d = d or {}
        return cls(
            id=_int(d.get("id")),
            title=str(d.get("title") or "Unknown Video"),
            version=str(d.get("version") or ""),
            artist=Artist.from_dict(d.get("artist")),
            artists=[Artist.from_dict(a) for a in (d.get("artists") or [])],
            duration=_int(d.get("duration")),
            explicit=bool(d.get("explicit")),
            release_date=str(d.get("releaseDate") or d.get("streamStartDate") or "")[:10],
            quality=str(d.get("quality") or "").upper(),
            stream_ready=d.get("streamReady", True) is not False,
            allow_streaming=d.get("allowStreaming", True) is not False,
        )

    @property
    def full_title(self) -> str:
        return with_version(self.title, self.version)

    @property
    def artist_names(self) -> str:
        return join_artists(self.artists, self.artist.name)

    @property
    def year(self) -> str:
        return self.release_date[:4] or "0000"

    @property
    def available(self) -> bool:
        return self.stream_ready and self.allow_streaming


@dataclass
class Playlist:
    uuid: str
    title: str
    description: str = ""
    number_of_tracks: int = 0
    duration: int = 0
    creator: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Playlist":
        d = d or {}
        creator = d.get("creator") or {}
        return cls(
            uuid=str(d.get("uuid") or ""),
            title=str(d.get("title") or "Playlist"),
            description=str(d.get("description") or ""),
            number_of_tracks=_int(d.get("numberOfTracks")),
            duration=_int(d.get("duration")),
            creator=str(creator.get("name") or "") if isinstance(creator, dict) else "",
        )


@dataclass
class Page:
    """Uma página de resultado paginado (``items``/``totalNumberOfItems``)."""

    items: list[dict]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total

    @classmethod
    def from_dict(cls, d: dict) -> "Page":
        d = d or {}
        return cls(
            items=list(d.get("items") or []),
            total=_int(d.get("totalNumberOfItems")),
            limit=_int(d.get("limit")),
            offset=_int(d.get("offset")),
        )


@dataclass
class Stream:
    """Manifesto decodificado, pronto para baixar."""

    track_id: int
    quality: str  # tier de fato entregue (ex.: "LOSSLESS") -- vem de audioQuality
    codec: str  # "flac", "aac", "mp4a.40.2"...
    urls: list[str]  # BTS: 1 URL. DASH: [init, seg1, seg2, ...]
    is_dash: bool = False
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    replay_gain: Optional[float] = None
    peak: Optional[float] = None
    album_replay_gain: Optional[float] = None
    album_peak: Optional[float] = None
    # "STEREO" ou "DOLBY_ATMOS" -- vem de audioMode, campo SEPARADO de
    # audioQuality na resposta da Tidal (ver playbackinfopostpaywall). Os
    # dois nunca se misturam: audioQuality nunca vale "DOLBY_ATMOS" (não é
    # um valor válido do enum), então checar `quality == "DOLBY_ATMOS"``
    # nunca detecta nada -- é audio_mode que tem que ser checado.
    audio_mode: str = ""

    @property
    def is_flac(self) -> bool:
        return "flac" in self.codec.lower()

    @property
    def extension(self) -> str:
        return "flac" if self.is_flac else "m4a"
