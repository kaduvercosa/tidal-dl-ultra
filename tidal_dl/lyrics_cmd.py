"""Comando de letras retroativas para uma biblioteca já baixada.

Não depende do login Tidal: lê artista/álbum/faixa dos metadados locais e usa
o mesmo fallback do download (Musixmatch -> LRCLIB). Arquivos que já possuem
letra são preservados por padrão; ``--force`` é a única opção que os substitui.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from tidal_dl import lyrics, ui

AUDIO_EXTENSIONS = {".flac", ".m4a", ".mp3", ".mp4"}


@dataclass
class AudioInfo:
    path: str
    artist: str = ""
    title: str = ""
    album: str = ""
    has_lyrics: bool = False


def _value(tags: Any, *keys: str) -> str:
    for key in keys:
        try:
            value = tags.get(key)
        except AttributeError:
            value = None
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def read_audio_info(path: str) -> AudioInfo:
    """Lê tags sem alterar o arquivo. Importa mutagen somente quando usado."""
    from mutagen import File  # noqa: PLC0415

    audio = File(path, easy=False)
    if audio is None:
        return AudioInfo(path)
    tags = audio.tags or {}
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".flac":
        artist = _value(tags, "ARTIST", "artist")
        title = _value(tags, "TITLE", "title")
        album = _value(tags, "ALBUM", "album")
        existing = _value(tags, "LYRICS", "UNSYNCEDLYRICS", "lyrics")
    elif suffix in (".m4a", ".mp4"):
        artist = _value(tags, "\xa9ART", "artist", "ARTIST")
        title = _value(tags, "\xa9nam", "title", "TITLE")
        album = _value(tags, "\xa9alb", "album", "ALBUM")
        existing = _value(tags, "\xa9lyr", "UNSYNCEDLYRICS", "lyrics")
    else:
        artist = _value(tags, "TPE1", "artist")
        title = _value(tags, "TIT2", "title")
        album = _value(tags, "TALB", "album")
        existing = bool(tags.getall("USLT") if hasattr(tags, "getall") else False)
    return AudioInfo(path, artist, title, album, bool(existing))


def write_lyrics(path: str, plain: str, synced: str) -> None:
    """Atualiza somente os campos de letra e preserva os demais metadados."""
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".flac":
        from mutagen.flac import FLAC  # noqa: PLC0415

        audio = FLAC(path)
        if synced:
            audio["LYRICS"] = [synced]
        if plain:
            audio["UNSYNCEDLYRICS"] = [plain]
        audio.save()
        return
    if suffix in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4, MP4FreeForm  # noqa: PLC0415

        audio = MP4(path)
        audio["\xa9lyr"] = [synced or plain]
        if plain:
            audio["----:com.apple.iTunes:UNSYNCEDLYRICS"] = [
                MP4FreeForm(plain.encode("utf-8"))
            ]
        audio.save()
        return
    if suffix == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError, USLT  # noqa: PLC0415

        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="und", desc="", text=synced or plain))
        tags.save(path)
        return
    raise ValueError(f"extensão sem suporte a letras: {suffix}")


def iter_audio_files(root: str):
    for folder, _dirs, names in os.walk(os.path.expanduser(root)):
        for name in sorted(names):
            if os.path.splitext(name)[1].lower() in AUDIO_EXTENSIONS:
                yield os.path.join(folder, name)


async def cmd_lyrics(
    args: Any,
    *,
    directory: str,
    http: Any = None,
    sleep=asyncio.sleep,
) -> int:
    root = os.path.expanduser(getattr(args, "DIR", None) or directory)
    if not os.path.isdir(root):
        ui.error(f"Pasta não encontrada: {root}")
        return 1
    own_http = http is None
    if own_http:
        from tidal_dl.net import create_client  # noqa: PLC0415

        http = create_client(requests_per_minute=60, retries=2, base_delay=1.0, sleep=sleep)
    mxm = lyrics.MusixmatchClient(http)
    force = bool(getattr(args, "force", False))
    dry = bool(getattr(args, "dry_run", False))
    limit = int(getattr(args, "limit", None) or 0)
    stats = {"found": 0, "updated": 0, "skipped": 0, "missing": 0, "failed": 0}
    try:
        files = list(iter_audio_files(root))
        if limit > 0:
            files = files[:limit]
        ui.banner("TIDAL-DL-ULTRA  ·  LETRAS RETROATIVAS")
        ui.kv("Pasta", root)
        ui.kv("Arquivos", len(files))
        ui.kv("Modo", "simulação" if dry else ("substituir existentes" if force else "preencher ausentes"))
        for index, path in enumerate(files, start=1):
            try:
                info = await asyncio.to_thread(read_audio_info, path)
                label = f"[{index}/{len(files)}] {info.artist} - {info.title}".strip(" -")
                if not info.artist or not info.title:
                    stats["missing"] += 1
                    ui.warn(f"{label}: tags ARTIST/TITLE ausentes")
                    continue
                if info.has_lyrics and not force:
                    stats["skipped"] += 1
                    continue
                data = await mxm.fetch(info.artist, info.title)
                source = str(data.get("_source") or "")
                if not data:
                    data = await lyrics.fetch_lrclib_lyrics(http, info.artist, info.title, info.album)
                    source = "LRCLIB" if data else ""
                plain = lyrics.plain_lyrics(data)
                synced = lyrics.synced_lyrics(data) or ""
                if not plain and not synced:
                    stats["missing"] += 1
                    ui.warn(f"{label}: nenhum provedor encontrou letra")
                    continue
                stats["found"] += 1
                if not dry:
                    await asyncio.to_thread(write_lyrics, path, plain, synced)
                    stats["updated"] += 1
                    ui.ok(f"{label}: letra atualizada ({source})")
                else:
                    ui.info(f"{label}: encontraria letra ({source})")
            except Exception as exc:
                stats["failed"] += 1
                ui.error(f"{path}: {exc}")
        ui.section("RESULTADO")
        for key, label in (
            ("found", "Com letra encontrada"), ("updated", "Atualizados"),
            ("skipped", "Já tinham letra"), ("missing", "Sem letra"),
            ("failed", "Falhas"),
        ):
            ui.kv(label, stats[key])
        return 1 if stats["failed"] else 0
    finally:
        if own_http and http is not None:
            await http.aclose()