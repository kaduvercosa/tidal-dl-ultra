"""Decodifica o manifesto de playback do Tidal e escolhe a qualidade.

Dois formatos chegam de ``playbackinfopostpaywall``:

  * ``application/vnd.tidal.bts``  -> JSON em base64, com UMA URL (arquivo inteiro);
  * ``application/dash+xml``       -> MPD em base64, com init + N segmentos;
  * ``application/vnd.tidal.emu`` / ``x-mpegURL`` -> M3U8 para vídeos.

POLÍTICA DE PROTEÇÃO
--------------------
Este projeto só baixa streams SEM criptografia. Se o Tidal devolver um stream
protegido (``encryptionType`` diferente de NONE), levantamos
``UnsupportedProtection`` e ``resolve_stream`` tenta a qualidade abaixo -- o
fluxo PKCE normal entrega LOSSLESS/HI_RES_LOSSLESS sem proteção. Não há
descriptografia aqui, de propósito.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import xml.etree.ElementTree as ET
from typing import Any, Optional
from urllib.parse import urljoin

from tidal_dl.constants import QUALITY_MAP
from tidal_dl.exceptions import (
    ForbiddenError,
    InvalidQuality,
    NonStreamable,
    PreviewOnly,
    UnsupportedProtection,
)
from tidal_dl.models import Stream

logger = logging.getLogger(__name__)

_NS = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}
_OPEN = {"", "NONE", "OFFLINEONLY"}

# Valores típicos por tier quando a resposta não informa profundidade/taxa.
_TIER_DEFAULTS = {
    "LOW": (16, 44100),
    "HIGH": (16, 44100),
    "LOSSLESS": (16, 44100),
    "HI_RES": (16, 44100),
    "HI_RES_LOSSLESS": (24, 96000),
}


def _is_protected(enc: Any) -> bool:
    norm = str(enc or "").replace("_", "").replace("-", "").upper()
    return norm not in _OPEN


def parse_dash(xml_text: str) -> tuple[str, list[str]]:
    """MPD do Tidal -> ``(codec, [init, seg1, ...])``.

    MPDs do Tidal são mínimos: 1 Representation com ``SegmentTemplate`` +
    ``SegmentTimeline`` (``r`` = repetições extras).
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise NonStreamable(f"MPD ilegível: {exc}") from exc
    rep = root.find(".//mpd:Representation", _NS)
    if rep is None:
        raise NonStreamable("MPD sem Representation")
    codec = (rep.get("codecs") or "").lower()
    tmpl = rep.find("mpd:SegmentTemplate", _NS)
    if tmpl is None:
        tmpl = root.find(".//mpd:SegmentTemplate", _NS)
    if tmpl is None:
        raise NonStreamable("MPD sem SegmentTemplate")
    init, media = tmpl.get("initialization"), tmpl.get("media")
    if not init or not media:
        raise NonStreamable("SegmentTemplate sem initialization/media")
    start = int(tmpl.get("startNumber", "1"))
    timeline = tmpl.find("mpd:SegmentTimeline", _NS)
    if timeline is None:
        raise NonStreamable("MPD sem SegmentTimeline")
    count = sum(1 + int(s.get("r", "0")) for s in timeline.findall("mpd:S", _NS))
    if count <= 0:
        raise NonStreamable("SegmentTimeline sem segmentos")
    return codec, [init] + [media.replace("$Number$", str(start + i)) for i in range(count)]


def parse_m3u8_playlist(content: str, base_url: str = "") -> list[str]:
    """Parse M3U8 content for segment URLs or master playlist variants."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]

    # If it's a master playlist, select highest quality variant
    if any("#EXT-X-STREAM-INF" in line for line in lines):
        selected_url = None
        for i, line in enumerate(lines):
            if "#EXT-X-STREAM-INF" in line and i + 1 < len(lines):
                selected_url = lines[i + 1]
        if selected_url:
            if not selected_url.startswith("http"):
                selected_url = urljoin(base_url, selected_url)
            return [selected_url]

    # Media playlist: extract segments
    urls = []
    for line in lines:
        if not line.startswith("#"):
            url = line if line.startswith("http") else urljoin(base_url, line)
            urls.append(url)
    return urls


def _decode_b64(text: str) -> bytes:
    try:
        return base64.b64decode(text)
    except (binascii.Error, ValueError) as exc:
        raise NonStreamable(f"manifest base64 inválido: {exc}") from exc


def stream_from_playback(info: dict, track_id: int) -> Stream:
    """Converte a resposta de playbackinfo em ``Stream`` (ou levanta erro claro)."""
    if str(info.get("assetPresentation") or "FULL").upper() != "FULL":
        raise PreviewOnly("Apenas prévia (PREVIEW) disponível: verifique a assinatura.")
    mime = str(info.get("manifestMimeType") or "")
    raw = _decode_b64(info.get("manifest") or "")
    tier = str(info.get("audioQuality") or "").upper()
    depth_d, rate_d = _TIER_DEFAULTS.get(tier, (16, 44100))
    common = dict(
        track_id=track_id,
        quality=tier,
        bit_depth=info.get("bitDepth") or depth_d,
        sample_rate=info.get("sampleRate") or rate_d,
        replay_gain=_f(info.get("trackReplayGain")),
        peak=_f(info.get("trackPeakAmplitude")),
        album_replay_gain=_f(info.get("albumReplayGain")),
        album_peak=_f(info.get("albumPeakAmplitude")),
    )

    if "dash" in mime:
        if _is_protected(info.get("encryptionType")):
            raise UnsupportedProtection(f"stream DASH protegido ({info.get('encryptionType')})")
        codec, urls = parse_dash(raw.decode("utf-8", errors="replace"))
        return Stream(codec=codec, urls=urls, is_dash=True, **common)

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise NonStreamable(f"manifest BTS ilegível: {exc}") from exc
    urls = manifest.get("urls") or []
    if not urls:
        restrictions = manifest.get("restrictions") or []
        code = restrictions[0].get("code") if restrictions and isinstance(restrictions[0], dict) else None
        raise NonStreamable(f"sem URL no manifesto ({code or 'restrito'})")
    if _is_protected(manifest.get("encryptionType")):
        raise UnsupportedProtection(f"stream protegido ({manifest.get('encryptionType')})")
    return Stream(codec=str(manifest.get("codecs") or ""), urls=list(urls), is_dash=False, **common)


def video_stream_from_playback(info: dict, video_id: int) -> Stream:
    """Converte a resposta de video playbackinfo em ``Stream``."""
    if str(info.get("assetPresentation") or "FULL").upper() != "FULL":
        raise PreviewOnly("Apenas prévia (PREVIEW) disponível para este vídeo.")
    mime = str(info.get("manifestMimeType") or "").lower()
    raw = _decode_b64(info.get("manifest") or "")

    if "dash" in mime:
        if _is_protected(info.get("encryptionType")):
            raise UnsupportedProtection(f"stream DASH de vídeo protegido ({info.get('encryptionType')})")
        codec, urls = parse_dash(raw.decode("utf-8", errors="replace"))
        return Stream(track_id=video_id, quality="VIDEO", codec=codec, urls=urls, is_dash=True)

    if "json" in mime or "bts" in mime or "emu" in mime:
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NonStreamable(f"manifest de vídeo ilegível: {exc}") from exc
        urls = manifest.get("urls") or []
        if not urls:
            restrictions = manifest.get("restrictions") or []
            code = restrictions[0].get("code") if restrictions and isinstance(restrictions[0], dict) else None
            raise NonStreamable(f"sem URL no manifesto de vídeo ({code or 'restrito'})")
        if _is_protected(manifest.get("encryptionType")):
            raise UnsupportedProtection(f"stream de vídeo protegido ({manifest.get('encryptionType')})")
        is_m3u = any(".m3u" in u for u in urls)
        return Stream(track_id=video_id, quality="VIDEO", codec=str(manifest.get("codecs") or "h264"), urls=list(urls), is_dash=False, is_m3u8=is_m3u)

    if "m3u8" in mime or "x-mpegurl" in mime:
        url_text = raw.decode("utf-8", errors="replace").strip()
        urls = [line.strip() for line in url_text.splitlines() if line.strip() and not line.startswith("#")]
        if not urls:
            urls = [url_text]
        return Stream(track_id=video_id, quality="VIDEO", codec="h264", urls=urls, is_m3u8=True)

    raise NonStreamable(f"tipo de manifesto de vídeo não suportado: {mime}")


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def resolve_stream(
    api: Any,
    track_id: int,
    quality: int,
    *,
    allow_fallback: bool = True,
    on_fallback: Any = None,
) -> Stream:
    """Pede o stream no tier ``quality`` e, se preciso, desce até um que sirva."""
    if quality not in QUALITY_MAP:
        raise InvalidQuality(f"qualidade inválida: {quality} (use 0-4)")
    last: Optional[Exception] = None
    for rank in range(quality, -1, -1):
        name = QUALITY_MAP[rank]
        try:
            info = await api.playback_info(track_id, name)
            return stream_from_playback(info, track_id)
        except PreviewOnly:
            raise
        except (ForbiddenError, NonStreamable) as exc:
            last = exc
            logger.debug("faixa %s indisponível em %s: %s", track_id, name, exc)
            if not allow_fallback or rank == 0:
                break
            if on_fallback:
                on_fallback(name, QUALITY_MAP[rank - 1], exc)
    assert last is not None
    if isinstance(last, ForbiddenError):
        raise NonStreamable(f"sem permissão para baixar a faixa {track_id}: {last}") from last
    raise last


async def resolve_video_stream(api: Any, video_id: int, quality: str = "HIGH") -> Stream:
    """Obtém o stream de vídeo do Tidal."""
    info = await api.video_playback_info(video_id, quality)
    return video_stream_from_playback(info, video_id)
