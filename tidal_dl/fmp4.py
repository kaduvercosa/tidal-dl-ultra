"""Remux FLAC-em-fMP4 -> FLAC nativo, em Python puro (sem ffmpeg).

POR QUE EXISTE
--------------
O Tidal entrega Lossless/Hi-Res Lossless como DASH: um segmento de
inicialização + N segmentos, todos fragmented-MP4 com quadros FLAC dentro.
Concatenados viram um ``.mp4`` que muitos players/taggers não aceitam (o
mutagen.flac falha em "sem cabeçalho FLAC"). A solução comum é
``ffmpeg -c:a copy`` -- mas o a-Shell/iOS nem sempre deixa o Python chamar o
ffmpeg. Como o conteúdo é só FLAC embrulhado, dá para desembrulhar na mão:

  1. o box ``dfLa`` do init traz os blocos de metadata FLAC (STREAMINFO);
  2. cada ``mdat`` traz quadros FLAC crus, em ordem;
  3. FLAC nativo = ``b"fLaC"`` + blocos de metadata + quadros.

Sem re-encode, sem perda, sem dependência. Lê/escreve em streaming (nunca
carrega o arquivo inteiro: um 24/192 passa de 100 MB e o iPhone não perdoa).
"""

from __future__ import annotations

import os
from typing import BinaryIO, Optional

from tidal_dl.exceptions import DownloadError

_CHUNK = 1 << 17


def _iter_metadata_blocks(data: bytes):
    """Percorre blocos de metadata FLAC: yield (tipo, corpo_completo_com_header)."""
    pos = 0
    while pos + 4 <= len(data):
        head = data[pos]
        length = int.from_bytes(data[pos + 1 : pos + 4], "big")
        end = pos + 4 + length
        if end > len(data):
            raise DownloadError("bloco de metadata FLAC truncado no box dfLa")
        yield head & 0x7F, data[pos + 4 : end]
        pos = end
        if head & 0x80:
            break


def build_flac_header(blocks: bytes) -> bytes:
    """``fLaC`` + blocos, garantindo a flag 'último' só no último bloco."""
    parts = list(_iter_metadata_blocks(blocks))
    if not parts or parts[0][0] != 0:
        raise DownloadError("dfLa sem STREAMINFO")
    out = bytearray(b"fLaC")
    for i, (btype, body) in enumerate(parts):
        last = 0x80 if i == len(parts) - 1 else 0
        out += bytes([last | btype]) + len(body).to_bytes(3, "big") + body
    return bytes(out)


def parse_streaminfo(header: bytes) -> dict:
    """Extrai sample_rate/canais/bit_depth/total_samples do STREAMINFO."""
    si = header[8:42]  # 'fLaC'(4) + hdr(4) + 34 bytes
    if len(si) < 34:
        return {}
    packed = int.from_bytes(si[10:18], "big")
    sample_rate = (packed >> 44) & 0xFFFFF
    channels = ((packed >> 41) & 0x7) + 1
    bits = ((packed >> 36) & 0x1F) + 1
    total = packed & 0xFFFFFFFFF
    return {"sample_rate": sample_rate, "channels": channels, "bit_depth": bits, "total_samples": total}


def flac_header_from_moov(moov: bytes) -> bytes:
    idx = moov.find(b"dfLa")
    if idx < 4:
        raise DownloadError("init do stream sem box dfLa (não é FLAC-em-MP4?)")
    size = int.from_bytes(moov[idx - 4 : idx], "big")
    payload = moov[idx + 4 : idx - 4 + size]
    return build_flac_header(payload[4:])  # pula version/flags do FullBox


def _read_box_header(f: BinaryIO, remaining: int) -> Optional[tuple[bytes, int, int]]:
    """Lê o header de um box: ``(tipo, tamanho_do_payload, tamanho_do_header)``."""
    hdr = f.read(8)
    if len(hdr) < 8:
        return None
    size = int.from_bytes(hdr[:4], "big")
    typ = hdr[4:8]
    hlen = 8
    if size == 1:
        ext = f.read(8)
        if len(ext) < 8:
            return None
        size, hlen = int.from_bytes(ext, "big"), 16
    elif size == 0:
        size = remaining
    if size < hlen:
        raise DownloadError("box MP4 corrompido")
    return typ, size - hlen, hlen


def remux_fmp4_flac(src: str, dst: str) -> dict:
    """Converte ``src`` (fMP4 com FLAC) em FLAC nativo ``dst``.

    Devolve ``{"sample_rate", "bit_depth", "channels", "total_samples"}``.
    Em erro remove ``dst`` parcial e levanta ``DownloadError``.
    """
    total = os.path.getsize(src)
    info: dict = {}
    wrote_audio = 0
    try:
        with open(src, "rb") as f, open(dst, "wb") as out:
            header_written = False
            while True:
                start = f.tell()
                box = _read_box_header(f, total - start)
                if box is None:
                    break
                typ, payload, _hlen = box
                if typ == b"moov":
                    header = flac_header_from_moov(f.read(payload))
                    info = parse_streaminfo(header)
                    out.write(header)
                    header_written = True
                elif typ == b"mdat":
                    if not header_written:
                        raise DownloadError("mdat antes do init (moov)")
                    left = payload
                    while left > 0:
                        chunk = f.read(min(_CHUNK, left))
                        if not chunk:
                            raise DownloadError("mdat truncado")
                        out.write(chunk)
                        left -= len(chunk)
                        wrote_audio += len(chunk)
                else:
                    f.seek(payload, os.SEEK_CUR)
        if not header_written or wrote_audio == 0:
            raise DownloadError("nenhum áudio encontrado no fMP4")
        return info
    except (OSError, DownloadError) as exc:
        try:
            os.remove(dst)
        except OSError:
            pass
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError(f"remux falhou: {exc}") from exc
