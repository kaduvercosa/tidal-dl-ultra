"""Download de vídeos HLS (M3U8) do Tidal: master/media playlist, AES-128, segmentos.

CONTEXTO
--------
Vídeo do Tidal é entregue como HLS (``.m3u8``), diferente do DASH usado no
áudio Hi-Res. Duas camadas de playlist:

  * **master** (``#EXT-X-STREAM-INF`` + uma URL por variante) -- lista as
    qualidades disponíveis (uma por faixa de bitrate);
  * **media** (lista de segmentos, cada um opcionalmente precedido por
    ``#EXT-X-KEY`` quando o CDN criptografa com AES-128).

Isso é decodificação de HLS "de manifesto público" -- os mesmos dados que
qualquer player recebe e usa para tocar o vídeo dentro da sessão autenticada
normal; não há nenhum DRM (Widevine/FairPlay) envolvido, só o AES-128 padrão
do próprio formato HLS (RFC 8216 §5.2), cuja chave vem no manifesto que a API
já entrega para quem tem permissão de reproduzir o conteúdo.

CONTAINER DE SAÍDA
-------------------
* Segmentos ``.ts`` (o caso comum): concatenados num único ``.ts``; se
  ``ffmpeg`` existir, o downloader faz um remux ``-c copy`` para ``.mp4``
  depois (função em ``downloader.py``). Sem ffmpeg, o ``.ts`` já é um
  arquivo de vídeo válido (toca no VLC e na maioria dos players).
* Segmentos fragmentados (``.m4s``/``.mp4``, com ``EXT-X-MAP`` de
  inicialização): concatenar init + segmentos EM ORDEM já produz um MP4
  fragmentado válido -- não precisa de remux.

Sem resumo entre execuções (ao contrário do áudio): um vídeo interrompido
recomeça do zero. Cada segmento individual tem retry.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from tidal_dl.exceptions import DownloadError, PermanentDownloadError
from tidal_dl.net import NetworkError
from tidal_dl.utils import retry_async

logger = logging.getLogger(__name__)

_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)')


class MissingDependencyError(DownloadError):
    """Falta um pacote opcional (ex.: ``cryptography``) para continuar."""


def _parse_attrs(line: str) -> dict:
    """``METHOD=AES-128,URI="k",IV=0x0102`` -> ``{"METHOD": "AES-128", ...}`` (aspas removidas)."""
    out = {}
    for key, raw in _ATTR_RE.findall(line):
        out[key] = raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
    return out


# ---------------------------------------------------------------------------
# Estruturas
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    method: str
    uri: str
    iv: Optional[bytes] = None  # None => deriva da sequência do segmento (RFC 8216 §5.2)


@dataclass(frozen=True)
class Segment:
    url: str
    sequence: int
    key: Optional[Key] = None


@dataclass
class MediaPlaylist:
    segments: list[Segment] = field(default_factory=list)
    init_url: Optional[str] = None  # EXT-X-MAP (fmp4)
    is_fragmented: bool = False


@dataclass(frozen=True)
class Variant:
    bandwidth: int
    url: str
    resolution: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master_playlist(text: str, base_url: str) -> list[Variant]:
    """Extrai as variantes (qualidades) de um master playlist."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = _parse_attrs(line.split(":", 1)[1] if ":" in line else "")
        if i + 1 >= len(lines) or lines[i + 1].startswith("#"):
            continue
        url = lines[i + 1]
        if not url.startswith("http"):
            url = urljoin(base_url, url)
        try:
            bandwidth = int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            bandwidth = 0
        variants.append(Variant(bandwidth, url, attrs.get("RESOLUTION", "")))
    if not variants:
        raise DownloadError("master playlist HLS sem variantes (#EXT-X-STREAM-INF)")
    return variants


_QUALITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def pick_variant(variants: list[Variant], quality: str = "HIGH") -> Variant:
    """Escolhe a variante pelo bitrate: ``LOW``/``MEDIUM``/``HIGH`` = menor/mediana/maior."""
    ordered = sorted(variants, key=lambda v: v.bandwidth)
    rank = _QUALITY_RANK.get((quality or "HIGH").upper(), 2)
    if rank == 0:
        return ordered[0]
    if rank == 1:
        return ordered[len(ordered) // 2]
    return ordered[-1]


def _iv_from_hex(raw: str) -> bytes:
    hexstr = raw[2:] if raw.lower().startswith("0x") else raw
    iv = bytes.fromhex(hexstr)
    if len(iv) != 16:
        raise DownloadError(f"IV de HLS com tamanho inválido: {len(iv)} bytes")
    return iv


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    """Extrai os segmentos (com sua chave, se houver) de um media playlist."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    playlist = MediaPlaylist()
    current_key: Optional[Key] = None
    sequence = 0
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            try:
                sequence = int(line.split(":", 1)[1])
            except (IndexError, ValueError):
                pass
        elif line.startswith("#EXT-X-MAP"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            uri = attrs.get("URI", "")
            if uri:
                playlist.init_url = uri if uri.startswith("http") else urljoin(base_url, uri)
                playlist.is_fragmented = True
        elif line.startswith("#EXT-X-KEY"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            if method == "NONE":
                current_key = None
            else:
                uri = attrs.get("URI", "")
                iv = _iv_from_hex(attrs["IV"]) if attrs.get("IV") else None
                current_key = Key(method, uri if uri.startswith("http") else urljoin(base_url, uri), iv)
        elif not line.startswith("#"):
            url = line if line.startswith("http") else urljoin(base_url, line)
            playlist.segments.append(Segment(url, sequence, current_key))
            sequence += 1
    if not playlist.is_fragmented and any(
        s.url.rsplit("?", 1)[0].endswith((".m4s", ".mp4", ".cmfv", ".cmfa")) for s in playlist.segments
    ):
        playlist.is_fragmented = True
    if not playlist.segments:
        raise DownloadError("media playlist HLS sem segmentos")
    return playlist


def derive_iv(key: Key, sequence: int) -> bytes:
    """IV explícito do manifesto, ou a sequência do segmento (RFC 8216 §5.2)."""
    return key.iv if key.iv is not None else sequence.to_bytes(16, "big")


def decrypt_segment(data: bytes, key_bytes: bytes, iv: bytes) -> bytes:
    """AES-128-CBC com padding PKCS7 (o método ``AES-128`` do HLS). Requer ``cryptography``."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as _padding
    except ImportError as exc:
        raise MissingDependencyError(
            "este vídeo tem segmentos criptografados (AES-128); instale o pacote "
            "'cryptography' para baixá-lo: pip install cryptography"
        ) from exc
    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = _padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ---------------------------------------------------------------------------
# Rede
# ---------------------------------------------------------------------------


async def _get_text(api_http: Any, url: str) -> str:
    resp = await api_http.request("GET", url)
    if resp.status in (401, 403, 404, 451):
        raise PermanentDownloadError(f"HTTP {resp.status} ao buscar playlist HLS")
    if resp.status >= 400:
        raise DownloadError(f"HTTP {resp.status} ao buscar playlist HLS")
    return resp.content.decode("utf-8", errors="replace")


async def _get_bytes(api_http: Any, url: str) -> bytes:
    async with api_http.stream(url) as r:
        if r.status in (401, 403, 404, 451):
            raise PermanentDownloadError(f"HTTP {r.status} em segmento HLS")
        if r.status >= 400:
            raise DownloadError(f"HTTP {r.status} em segmento HLS")
        buf = bytearray()
        async for chunk in r.iter_chunks(1 << 17):
            buf += chunk
        if not buf:
            raise DownloadError("segmento HLS vazio")
        return bytes(buf)


async def resolve_media_playlist(api_http: Any, top_url: str, quality: str = "HIGH") -> MediaPlaylist:
    """Segue master -> media (se preciso) e devolve os segmentos já resolvidos."""
    text = await _get_text(api_http, top_url)
    if is_master_playlist(text):
        variant = pick_variant(parse_master_playlist(text, top_url), quality)
        text = await _get_text(api_http, variant.url)
        base = variant.url
    else:
        base = top_url
    return parse_media_playlist(text, base)


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------

ProgressBar = Any  # objeto com .update(n) -- ver tidal_dl.progress


async def download_hls(
    api_http: Any,
    top_url: str,
    dest_path: str,
    *,
    quality: str = "HIGH",
    retries: int = 3,
    sleep: Callable[[float], Any] = asyncio.sleep,
    bar: Optional[ProgressBar] = None,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
    abort_check: Optional[Callable[[], bool]] = None,
) -> dict:
    """Baixa (e decripta se preciso) um vídeo HLS inteiro para ``dest_path``.

    Devolve ``{"segments", "bytes", "fragmented"}``. ``dest_path`` já é um MP4
    fragmentado válido quando ``fragmented`` é True; senão é um ``.ts`` cru
    (o remux para ``.mp4`` via ffmpeg, se houver, é feito por quem chama).
    """
    playlist = await resolve_media_playlist(api_http, top_url, quality)
    key_cache: dict[str, bytes] = {}
    total_bytes = 0

    async def fetch_key(key: Key) -> bytes:
        if key.uri not in key_cache:
            key_cache[key.uri] = await _get_bytes(api_http, key.uri)
        return key_cache[key.uri]

    async def one_segment(seg: Segment) -> bytes:
        raw = await _get_bytes(api_http, seg.url)
        if seg.key is None or seg.key.method == "NONE":
            return raw
        if seg.key.method != "AES-128":
            raise DownloadError(f"método de criptografia HLS não suportado: {seg.key.method}")
        key_bytes = await fetch_key(seg.key)
        return decrypt_segment(raw, key_bytes, derive_iv(seg.key, seg.sequence))

    with open(dest_path, "wb") as out:
        if playlist.init_url:
            out.write(await _get_bytes(api_http, playlist.init_url))

        for seg in playlist.segments:
            if abort_check and abort_check():
                raise KeyboardInterrupt

            def retry_cb(n: int, exc: BaseException, _seg=seg) -> None:
                if on_retry:
                    on_retry(n, exc)

            data = await retry_async(
                lambda _seg=seg: one_segment(_seg),
                attempts=retries,
                base_delay=1.5,
                retry_on=(NetworkError, DownloadError, OSError),
                give_up_on=(asyncio.CancelledError, KeyboardInterrupt, PermanentDownloadError, MissingDependencyError),
                sleep=sleep,
                on_retry=retry_cb,
            )
            out.write(data)
            total_bytes += len(data)
            if bar is not None:
                bar.update(1)

    if total_bytes == 0:
        raise DownloadError("download de vídeo vazio")
    return {"segments": len(playlist.segments), "bytes": total_bytes, "fragmented": playlist.is_fragmented}
