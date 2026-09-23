"""Letras do Tidal: texto simples e LRC sincronizado."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

LRCLIB_URL = "https://lrclib.net/api/get"

_LRC_LINE = re.compile(r"^\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]")


def plain_lyrics(resp: dict) -> str:
    """Texto sem tempos. Se só há LRC, remove as marcas de tempo."""
    text = str((resp or {}).get("lyrics") or "").strip()
    if text:
        return text
    lrc = synced_lyrics(resp)
    if not lrc:
        return ""
    lines = [re.sub(r"^\[[^\]]*\]\s*", "", ln) for ln in lrc.splitlines()]
    return "\n".join(ln for ln in lines if not ln.startswith("[") or ln.strip())


def synced_lyrics(resp: dict) -> Optional[str]:
    """LRC (``[mm:ss.xx] letra``) ou None se não houver algo que pareça LRC."""
    sub = str((resp or {}).get("subtitles") or "").strip()
    if sub and any(_LRC_LINE.match(ln.strip()) for ln in sub.splitlines()):
        return sub
    return None


async def fetch_lrclib_lyrics(http: Any, artist: str, title: str, album: str = "") -> dict:
    """Busca letra no LRCLIB (banco aberto e gratuito de letras, sem chave de API).

    Usado como reforço quando o Tidal não tem letra para a faixa -- LRCLIB é
    mantido justamente para esse tipo de consulta por outros programas.
    Assíncrono (reaproveita o ``HttpClient`` do Tidal, com seu rate-limit e
    retry) e NUNCA levanta: qualquer falha vira ``{}``, como ``api.get_lyrics``.
    Devolve no mesmo formato de ``api.get_lyrics`` (``lyrics``/``subtitles``),
    para servir direto a ``plain_lyrics``/``synced_lyrics``.
    """
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    try:
        resp = await http.request("GET", LRCLIB_URL, params=params)
        if resp.status != 200 and album:
            resp = await http.request(
                "GET", LRCLIB_URL, params={"artist_name": artist, "track_name": title}
            )
        if resp.status != 200:
            return {}
        data = resp.json()
    except Exception as exc:  # letra nunca pode derrubar o download
        logger.debug("LRCLIB indisponível para %s - %s: %s", artist, title, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {"lyrics": data.get("plainLyrics") or "", "subtitles": data.get("syncedLyrics") or ""}
