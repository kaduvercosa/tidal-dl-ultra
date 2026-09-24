"""Decodifica o manifesto de playback do Tidal e escolhe a qualidade.

Dois formatos chegam de ``playbackinfopostpaywall``:

  * ``application/vnd.tidal.bts``  -> JSON em base64, com UMA URL (arquivo inteiro);
  * ``application/dash+xml``       -> MPD em base64, com init + N segmentos.

POLÍTICA DE PROTEÇÃO E FALLBACK
--------------------------------
Este projeto só baixa streams SEM criptografia. Se o Tidal devolver um stream
protegido (``encryptionType`` diferente de NONE), levantamos
``UnsupportedProtection`` e ``resolve_stream`` tenta a qualidade abaixo -- o
fluxo PKCE normal entrega LOSSLESS/HI_RES_LOSSLESS sem proteção. Não há
descriptografia aqui, de propósito.

Fallback só é considerado para uma indisponibilidade explícita do tier
(``403``, ausência de URL ou proteção não suportada). Erro de rede,
autenticação, rate limit e manifesto inválido propagam sem tentar mascarar o
problema com uma qualidade menor.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import xml.etree.ElementTree as ET
from typing import Any, Optional

from tidal_dl.constants import QUALITY_BY_NAME, QUALITY_MAP
from tidal_dl.exceptions import (
    ForbiddenError,
    InvalidQuality,
    ManifestError,
    NonStreamable,
    PreviewOnly,
    QualityUnavailable,
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
        raise ManifestError(f"MPD ilegível: {exc}") from exc
    rep = root.find(".//mpd:Representation", _NS)
    if rep is None:
        raise ManifestError("MPD sem Representation")
    codec = (rep.get("codecs") or "").lower()
    tmpl = rep.find("mpd:SegmentTemplate", _NS)
    if tmpl is None:
        tmpl = root.find(".//mpd:SegmentTemplate", _NS)
    if tmpl is None:
        raise ManifestError("MPD sem SegmentTemplate")
    init, media = tmpl.get("initialization"), tmpl.get("media")
    if not init or not media:
        raise ManifestError("SegmentTemplate sem initialization/media")
    start = int(tmpl.get("startNumber", "1"))
    timeline = tmpl.find("mpd:SegmentTimeline", _NS)
    if timeline is None:
        raise ManifestError("MPD sem SegmentTimeline")
    count = sum(1 + int(s.get("r", "0")) for s in timeline.findall("mpd:S", _NS))
    if count <= 0:
        raise ManifestError("SegmentTimeline sem segmentos")
    return codec, [init] + [media.replace("$Number$", str(start + i)) for i in range(count)]


def _decode_b64(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestError(f"manifest base64 inválido: {exc}") from exc


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
        raise ManifestError(f"manifest BTS ilegível: {exc}") from exc
    urls = manifest.get("urls") or []
    if not urls:
        restrictions = manifest.get("restrictions") or []
        code = restrictions[0].get("code") if restrictions and isinstance(restrictions[0], dict) else None
        raise QualityUnavailable(f"sem URL no manifesto ({code or 'restrito'})")
    if _is_protected(manifest.get("encryptionType")):
        raise UnsupportedProtection(f"stream protegido ({manifest.get('encryptionType')})")
    return Stream(codec=str(manifest.get("codecs") or ""), urls=list(urls), is_dash=False, **common)


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
    preferred_tier: Optional[str] = None,
) -> Stream:
    """Pede o stream no tier ``quality`` e, se preciso, desce até um que sirva.

    Desce em: sem permissão (403), não-streamable, protegido. NÃO desce em
    erro de rede/autenticação/rate limit (esses propagam).

    ``preferred_tier`` (ex.: ``"DOLBY_ATMOS"``): tentado ANTES da escada
    numérica normal, só quando a própria faixa já informa esse tier como o
    audioQuality dela -- ver ``DOLBY_ATMOS_TIER`` em constants.py e o
    chamador em downloader.py. A escada normal (0-4) nunca pede
    "DOLBY_ATMOS" sozinha (não está em QUALITY_MAP), então sem isso o Tidal
    fica livre pra devolver um downmix estéreo comum quando um tier normal é
    pedido pra uma faixa que só existe "de verdade" em Atmos. Se o tier
    preferido falhar (não aceito, indisponível, protegido), cai pra escada
    normal sem barulho -- não é fatal, é só uma tentativa a mais.
    """
    if quality not in QUALITY_MAP:
        raise InvalidQuality(f"qualidade inválida: {quality} (use 0-4)")
    if preferred_tier:
        try:
            info = await api.playback_info(track_id, preferred_tier)
            return stream_from_playback(info, track_id)
        except PreviewOnly:
            raise
        except (ForbiddenError, NonStreamable) as exc:
            if isinstance(exc, ManifestError):
                raise
            logger.debug("tier preferido %s indisponível para %s: %s", preferred_tier, track_id, exc)
    last: Optional[Exception] = None
    for rank in range(quality, -1, -1):
        name = QUALITY_MAP[rank]
        try:
            info = await api.playback_info(track_id, name)
            stream = stream_from_playback(info, track_id)
            returned_rank = QUALITY_BY_NAME.get((stream.quality or "").upper())
            if returned_rank is not None and returned_rank < rank:
                raise QualityUnavailable(
                    f"tier {name} devolveu {stream.quality}, abaixo do solicitado"
                )
            return stream
        except PreviewOnly:
            raise  # descer de qualidade não resolve prévia
        except (ForbiddenError, NonStreamable) as exc:
            if isinstance(exc, ManifestError):
                raise
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


async def resolve_video_playback(api: Any, video_id: int, quality: str = "HIGH") -> str:
    """Pede o playback info do vídeo e devolve a URL do manifesto HLS (top-level).

    O manifesto (``manifestMimeType`` ``application/vnd.tidal.emu``) é, como no
    áudio BTS, um JSON em base64 com ``{"urls": [...]}`` -- só que a URL aqui é
    de um ``.m3u8`` (master ou media playlist), não de um arquivo de áudio.
    """
    info = await api.video_playback_info(video_id, quality)
    if str(info.get("assetPresentation") or "FULL").upper() != "FULL":
        raise PreviewOnly("Apenas prévia (PREVIEW) disponível para este vídeo.")
    raw = _decode_b64(info.get("manifest") or "")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise NonStreamable(f"manifesto de vídeo ilegível: {exc}") from exc
    urls = manifest.get("urls") or []
    if not urls:
        raise NonStreamable("sem URL no manifesto de vídeo")
    return urls[0]
