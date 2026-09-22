"""Letras do Tidal: texto simples e LRC sincronizado."""

from __future__ import annotations

import re
from typing import Optional

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
