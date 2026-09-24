"""Inspeção local de qualidade, tags e letras."""

from __future__ import annotations

import json
import os
from typing import Any

from tidal_dl import ui
from tidal_dl.lyrics_cmd import iter_audio_files, read_audio_info


def inspect_audio(path: str) -> dict:
    from mutagen import File  # noqa: PLC0415

    info = read_audio_info(path)
    audio = File(path, easy=False)
    result: dict[str, Any] = {
        "path": path,
        "artist": info.artist,
        "title": info.title,
        "album": info.album,
        "has_lyrics": info.has_lyrics,
        "format": os.path.splitext(path)[1].lower().lstrip("."),
    }
    if audio is None:
        result["error"] = "formato não reconhecido"
        return result
    codec = getattr(audio.info, "codec", None)
    sample_rate = getattr(audio.info, "sample_rate", None)
    bits = getattr(audio.info, "bits_per_sample", None)
    bitrate = getattr(audio.info, "bitrate", None)
    result.update({
        "codec": str(codec or ""),
        "sample_rate": sample_rate,
        "bit_depth": bits,
        "bitrate": bitrate,
        "duration": getattr(audio.info, "length", None),
    })
    return result


async def cmd_inspect(args: Any, *, directory: str) -> int:
    root = os.path.expanduser(getattr(args, "DIR", None) or directory)
    if not os.path.isdir(root):
        ui.error(f"Pasta não encontrada: {root}")
        return 1
    files = list(iter_audio_files(root))
    limit = int(getattr(args, "limit", None) or 0)
    if limit > 0:
        files = files[:limit]
    rows = []
    for path in files:
        try:
            rows.append(inspect_audio(path))
        except Exception as exc:
            rows.append({"path": path, "error": str(exc)})
    if getattr(args, "json", False):
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return 1 if any(row.get("error") for row in rows) else 0
    ui.banner("TIDAL-DL-ULTRA  ·  INSPEÇÃO LOCAL")
    for row in rows:
        name = f"{row.get('artist') or '?'} - {row.get('title') or os.path.basename(row['path'])}"
        if row.get("error"):
            ui.error(f"{name}: {row['error']}")
            continue
        quality = f"{row.get('bit_depth') or '?'}bit/{(row.get('sample_rate') or 0) / 1000:g}kHz"
        lyric = "letra" if row.get("has_lyrics") else "sem letra"
        ui.emit_always(f"{quality:<18} {row.get('codec') or row['format']:<10} {lyric:<10} {name}")
    ui.info(f"\n{len(rows)} arquivo(s) inspecionado(s).")
    return 1 if any(row.get("error") for row in rows) else 0