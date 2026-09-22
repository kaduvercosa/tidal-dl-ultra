"""Sincronização de favoritos: conta Tidal ⇄ catálogo local (``library.db``).

Mesma ideia do ``favorites_sync`` do qobuz-dl-ultra (que veio do libsync):
diff de novos/removidos, histórico de execuções e download opcional do que
falta. Ponto de segurança idêntico: só marca *removidos* quando a paginação
foi comprovadamente completa (itens coletados == ``totalNumberOfItems``), e
qualquer falha de rede/token PROPAGA em vez de virar "lista vazia".

Este módulo não imprime nada; a camada de terminal é ``library_cmd``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Iterable, Optional

from tidal_dl import db as dbm
from tidal_dl.library_db import (
    STATUS_COMPLETE,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_NOT_DOWNLOADED,
    STATUS_QUEUED,
    LibraryDB,
)
from tidal_dl.library_scan import mark_album_downloaded
from tidal_dl.models import Album
from tidal_dl.sentinel import has_sentinel
from tidal_dl.utils import cover_url

logger = logging.getLogger(__name__)

SOURCE = "tidal"
PAGE_SIZE = 100
MAX_PAGES = 500


class FavoritesFetchError(RuntimeError):
    """Falha ao listar favoritos (rede, token, permissão...)."""


async def fetch_all_favorite_albums(api: Any, *, page_size: int = PAGE_SIZE) -> tuple[list[dict], Optional[int]]:
    """Pagina os álbuns favoritos. Devolve ``(itens_crus, total_da_api)``."""
    items: list[dict] = []
    total: Optional[int] = None
    offset = 0
    for _ in range(MAX_PAGES):
        try:
            page = await api.favorite_albums_page(limit=page_size, offset=offset)
        except Exception as exc:
            raise FavoritesFetchError(f"falha ao listar favoritos: {exc}") from exc
        if total is None and page.total:
            total = page.total
        items.extend(page.items)
        if not page.items:
            break
        if total is not None and len(items) >= total:
            break
        offset += len(page.items)
    if total is None and items == []:
        total = 0
    return items, total


def extract_album_data(entry: dict) -> Optional[dict]:
    """``{created, item:{album}}`` (ou o álbum direto) -> campos do catálogo."""
    raw = entry.get("item") if isinstance(entry.get("item"), dict) else entry
    album = Album.from_dict(raw)
    if not album.id:
        return None
    return {
        "source_album_id": str(album.id),
        "title": album.full_title,
        "artist": album.album_artist,
        "track_count": album.number_of_tracks or None,
        # bit_depth/sample_rate ficam desconhecidos de propósito: o Tidal só diz
        # a qualidade MÁXIMA do álbum, não a que você baixou -- gravá-la geraria
        # falsos "bit_depth_mismatch" no scan.
        "release_date": album.release_date or None,
        "label": album.label or None,
        "upc": album.upc or None,
        "duration_seconds": album.duration or None,
        "cover_url": cover_url(album.cover, 640),
        "added_to_library_at": str(entry.get("created") or "") or None,
    }


async def refresh_library(lib: LibraryDB, api: Any, *, source: str = SOURCE, dry_run: bool = False) -> dict:
    items, api_total = await fetch_all_favorite_albums(api)
    parsed = [p for p in (extract_album_data(i) for i in items) if p]
    ids = [p["source_album_id"] for p in parsed]
    complete = api_total is not None and len(items) >= api_total

    new_albums: list[dict] = []
    for p in parsed:
        existing = await asyncio.to_thread(lib.get_album_by_source_id, source, p["source_album_id"])
        if existing is None:
            new_albums.append(p)
        if not dry_run:
            fields = {k: v for k, v in p.items() if k not in ("source_album_id", "title", "artist")}
            await asyncio.to_thread(
                lib.upsert_album, source, p["source_album_id"], p["title"], p["artist"], **fields
            )

    removed: list[dict] = []
    if complete:
        if dry_run:
            present = set(ids)
            removed = [
                a for a in await asyncio.to_thread(lib.get_albums, source, include_removed=False)
                if a["source_album_id"] not in present
            ]
        else:
            removed = await asyncio.to_thread(lib.mark_removed, source, ids)

    return {
        "total": len(parsed),
        "new": len(new_albums),
        "new_ids": [a["source_album_id"] for a in new_albums],
        "new_albums": new_albums,
        "removed": len(removed),
        "removed_albums": removed,
        "complete": complete,
    }


def lookup_saved_path(db_path: Optional[str], album_id: Any) -> Optional[str]:
    if not db_path:
        return None
    rec = dbm.get_record(db_path, album_id, "album")
    return (rec or {}).get("saved_path") or None


DownloadFn = Callable[[str], Awaitable[bool]]
ProgressFn = Callable[[int, int, dict], None]


def pick_download_targets(
    lib: LibraryDB,
    *,
    source: str = SOURCE,
    only_ids: Optional[Iterable[str]] = None,
    missing: bool = False,
    limit: Optional[int] = None,
) -> list[dict]:
    if only_ids is not None:
        wanted = set(only_ids)
        rows = [a for a in lib.get_albums(source, include_removed=False) if a["source_album_id"] in wanted]
    elif missing:
        rows = [a for a in lib.get_albums(source, include_removed=False)
                if a["download_status"] != STATUS_COMPLETE]
    else:
        rows = []
    return rows[:limit] if limit else rows


async def download_albums(
    lib: LibraryDB,
    albums: list[dict],
    download_fn: DownloadFn,
    *,
    downloads_db: Optional[str] = None,
    sentinel_enabled: bool = True,
    progress: Optional[ProgressFn] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> dict:
    done = failed = 0
    failures: list[dict] = []
    for a in albums:
        await asyncio.to_thread(lib.update_status, a["id"], STATUS_QUEUED)

    for i, a in enumerate(albums, start=1):
        if stop_event is not None and stop_event.is_set():
            await asyncio.to_thread(lib.update_status, a["id"], STATUS_NOT_DOWNLOADED)
            continue
        if progress:
            progress(i, len(albums), a)
        await asyncio.to_thread(lib.update_status, a["id"], STATUS_DOWNLOADING)
        try:
            ok = bool(await download_fn(a["source_album_id"]))
        except asyncio.CancelledError:
            await asyncio.to_thread(lib.update_status, a["id"], STATUS_NOT_DOWNLOADED)
            raise
        except Exception as exc:
            logger.exception("sync: falha baixando %s", a["source_album_id"])
            ok = False
            failures.append({"album_id": a["id"], "title": a["title"], "error": str(exc)})
        if ok:
            folder = lookup_saved_path(downloads_db, a["source_album_id"])
            if folder and os.path.isdir(folder):
                write_it = sentinel_enabled and not await asyncio.to_thread(has_sentinel, folder)
                await asyncio.to_thread(mark_album_downloaded, lib, a["id"], folder=folder,
                                        sentinel_enabled=write_it)
            else:
                await asyncio.to_thread(lib.set_download_state, a["id"], downloaded=True,
                                        status=STATUS_COMPLETE)
            done += 1
        else:
            await asyncio.to_thread(lib.update_status, a["id"], STATUS_FAILED)
            failed += 1
    return {"downloaded": done, "failed": failed, "failures": failures}


async def run_sync(
    lib: LibraryDB,
    api: Any,
    *,
    source: str = SOURCE,
    download_new: bool = False,
    download_missing: bool = False,
    download_fn: Optional[DownloadFn] = None,
    downloads_db: Optional[str] = None,
    sentinel_enabled: bool = True,
    limit: Optional[int] = None,
    dry_run: bool = False,
    progress: Optional[ProgressFn] = None,
    confirm: Optional[Callable[[list[dict]], Awaitable[bool]]] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> dict:
    run_id: Optional[int] = None
    if not dry_run:
        run_id = await asyncio.to_thread(lib.create_sync_run, source)
    try:
        refresh = await refresh_library(lib, api, source=source, dry_run=dry_run)
        targets: list[dict] = []
        if not dry_run and download_fn is not None:
            if download_missing:
                targets = pick_download_targets(lib, source=source, missing=True, limit=limit)
            elif download_new:
                targets = pick_download_targets(lib, source=source, only_ids=refresh["new_ids"], limit=limit)
        dl = {"downloaded": 0, "failed": 0, "failures": []}
        if targets:
            if confirm is not None and not await confirm(targets):
                targets = []
            else:
                dl = await download_albums(
                    lib, targets, download_fn,  # type: ignore[arg-type]
                    downloads_db=downloads_db, sentinel_enabled=sentinel_enabled,
                    progress=progress, stop_event=stop_event,
                )
        if run_id is not None:
            await asyncio.to_thread(
                lib.complete_sync_run, run_id, albums_found=refresh["total"], albums_new=refresh["new"],
                albums_removed=refresh["removed"], albums_downloaded=dl["downloaded"],
            )
        return {"status": "complete", "run_id": run_id, "dry_run": dry_run,
                "refresh": refresh, "targets": len(targets), "download": dl}
    except asyncio.CancelledError:
        if run_id is not None:
            await asyncio.to_thread(lib.interrupt_sync_run, run_id)
        raise
    except Exception:
        if run_id is not None:
            await asyncio.to_thread(lib.fail_sync_run, run_id)
        raise
