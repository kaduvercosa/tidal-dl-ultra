"""Download de vídeos HLS (M3U8): playlists, AES-128 e segmentos."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from tidal_dl.exceptions import DownloadError, PermanentDownloadError
from tidal_dl.net import NetworkError
from tidal_dl.utils import human_size, retry_async

logger = logging.getLogger(__name__)
_ATTR_RE = re.compile(r'([A-Z0-9-]+)=("(?:[^"]*)"|[^,]*)')


class MissingDependencyError(DownloadError):
    """Falta um pacote opcional, como cryptography."""


def _parse_attrs(line: str) -> dict:
    return {
        key: raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw
        for key, raw in _ATTR_RE.findall(line)
    }


@dataclass(frozen=True)
class Key:
    method: str
    uri: str
    iv: Optional[bytes] = None


@dataclass(frozen=True)
class Segment:
    url: str
    sequence: int
    key: Optional[Key] = None


@dataclass
class MediaPlaylist:
    segments: list[Segment] = field(default_factory=list)
    init_url: Optional[str] = None
    is_fragmented: bool = False


@dataclass(frozen=True)
class Variant:
    bandwidth: int
    url: str
    resolution: str = ""


def is_master_playlist(text: str) -> bool:
    return "#EXT-X-STREAM-INF" in text


def parse_master_playlist(text: str, base_url: str) -> list[Variant]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants = []
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = _parse_attrs(line.split(":", 1)[1] if ":" in line else "")
        if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
            continue
        url = lines[index + 1]
        variants.append(Variant(
            int(attrs.get("BANDWIDTH", "0") or 0),
            url if url.startswith("http") else urljoin(base_url, url),
            attrs.get("RESOLUTION", ""),
        ))
    if not variants:
        raise DownloadError("master playlist HLS sem variantes (#EXT-X-STREAM-INF)")
    return variants


_QUALITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def pick_variant(variants: list[Variant], quality: str = "HIGH") -> Variant:
    ordered = sorted(variants, key=lambda variant: variant.bandwidth)
    rank = _QUALITY_RANK.get((quality or "HIGH").upper(), 2)
    if rank == 0:
        return ordered[0]
    if rank == 1:
        return ordered[len(ordered) // 2]
    return ordered[-1]


def _iv_from_hex(raw: str) -> bytes:
    value = raw[2:] if raw.lower().startswith("0x") else raw
    iv = bytes.fromhex(value)
    if len(iv) != 16:
        raise DownloadError(f"IV de HLS com tamanho inválido: {len(iv)} bytes")
    return iv


def parse_media_playlist(text: str, base_url: str) -> MediaPlaylist:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
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
            uri = _parse_attrs(line.split(":", 1)[1]).get("URI", "")
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
        segment.url.rsplit("?", 1)[0].endswith((".m4s", ".mp4", ".cmfv", ".cmfa"))
        for segment in playlist.segments
    ):
        playlist.is_fragmented = True
    if not playlist.segments:
        raise DownloadError("media playlist HLS sem segmentos")
    return playlist


def derive_iv(key: Key, sequence: int) -> bytes:
    return key.iv if key.iv is not None else sequence.to_bytes(16, "big")


def decrypt_segment(data: bytes, key_bytes: bytes, iv: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as pkcs7
    except ImportError as exc:
        raise MissingDependencyError(
            "este vídeo tem segmentos criptografados (AES-128); instale "
            "'cryptography' para baixá-lo: pip install cryptography"
        ) from exc
    decryptor = Cipher(algorithms.AES(key_bytes), modes.CBC(iv)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = pkcs7.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


async def _get_text(api_http: Any, url: str) -> str:
    response = await api_http.request("GET", url)
    if response.status in (401, 403, 404, 451):
        raise PermanentDownloadError(f"HTTP {response.status} ao buscar playlist HLS")
    if response.status >= 400:
        raise DownloadError(f"HTTP {response.status} ao buscar playlist HLS")
    return response.content.decode("utf-8", errors="replace")


async def _get_bytes(api_http: Any, url: str) -> bytes:
    async with api_http.stream(url) as response:
        if response.status in (401, 403, 404, 451):
            raise PermanentDownloadError(f"HTTP {response.status} em segmento HLS")
        if response.status >= 400:
            raise DownloadError(f"HTTP {response.status} em segmento HLS")
        buffer = bytearray()
        async for chunk in response.iter_chunks(1 << 17):
            buffer += chunk
    if not buffer:
        raise DownloadError("segmento HLS vazio")
    return bytes(buffer)


async def resolve_media_playlist(api_http: Any, top_url: str, quality: str = "HIGH") -> MediaPlaylist:
    text = await _get_text(api_http, top_url)
    if is_master_playlist(text):
        variant = pick_variant(parse_master_playlist(text, top_url), quality)
        text = await _get_text(api_http, variant.url)
        base_url = variant.url
    else:
        base_url = top_url
    return parse_media_playlist(text, base_url)


async def download_hls(
    api_http: Any,
    top_url: str,
    dest_path: str,
    *,
    quality: str = "HIGH",
    retries: int = 3,
    sleep: Callable[[float], Any] = asyncio.sleep,
    bar: Optional[Any] = None,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
    abort_check: Optional[Callable[[], bool]] = None,
    playlist: Optional[MediaPlaylist] = None,
) -> dict:
    if playlist is None:
        playlist = await resolve_media_playlist(api_http, top_url, quality)
    key_cache: dict[str, bytes] = {}
    total_bytes = 0

    async def fetch_key(key: Key) -> bytes:
        if key.uri not in key_cache:
            key_cache[key.uri] = await _get_bytes(api_http, key.uri)
        return key_cache[key.uri]

    async def one_segment(segment: Segment) -> bytes:
        raw = await _get_bytes(api_http, segment.url)
        if segment.key is None or segment.key.method == "NONE":
            return raw
        if segment.key.method != "AES-128":
            raise DownloadError(f"método de criptografia HLS não suportado: {segment.key.method}")
        return decrypt_segment(raw, await fetch_key(segment.key), derive_iv(segment.key, segment.sequence))

    with open(dest_path, "wb") as output:
        if playlist.init_url:
            output.write(await _get_bytes(api_http, playlist.init_url))
        for segment in playlist.segments:
            if abort_check and abort_check():
                raise KeyboardInterrupt

            def retry_callback(number: int, exc: BaseException) -> None:
                if on_retry:
                    on_retry(number, exc)

            data = await retry_async(
                lambda: one_segment(segment), attempts=retries, base_delay=1.5,
                retry_on=(NetworkError, DownloadError, OSError),
                give_up_on=(asyncio.CancelledError, KeyboardInterrupt,
                            PermanentDownloadError, MissingDependencyError),
                sleep=sleep, on_retry=retry_callback,
            )
            output.write(data)
            total_bytes += len(data)
            if bar is not None:
                bar.update(1)
                set_postfix = getattr(bar, "set_postfix_str", None)
                if set_postfix:
                    set_postfix(human_size(total_bytes))

    if total_bytes == 0:
        raise DownloadError("download de vídeo vazio")
    return {"segments": len(playlist.segments), "bytes": total_bytes, "fragmented": playlist.is_fragmented}
