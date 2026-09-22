"""Banco de dedup dos downloads (``tidal_dl.db``).

Só ``sqlite3`` da biblioteca padrão (sem aiosqlite: uma dependência a menos no
a-Shell). Funções síncronas + wrappers ``a_*`` que rodam em thread.

Chave: (id, media_type). Um álbum baixado em qualidade menor NÃO é rebaixado
nem re-baixado, mas se você pedir uma qualidade MAIOR o registro é ignorado
(``is_downloaded(..., quality=N)`` só conta quando a qualidade salva >= N).
Registro cujo caminho sumiu do disco é descartado sozinho (o arquivo foi
movido/apagado: baixa de novo).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    quality INTEGER NOT NULL DEFAULT 0,
    file_format TEXT,
    bit_depth INTEGER,
    sampling_rate REAL,
    saved_path TEXT,
    artist TEXT,
    album TEXT,
    title TEXT,
    release_date TEXT,
    downloaded_at TEXT NOT NULL,
    PRIMARY KEY (id, media_type)
);
"""


@contextlib.contextmanager
def _conn(path: str, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path: str) -> None:
    with _conn(path) as c:
        c.executescript(SCHEMA)


def get_record(path: str, item_id: Any, media_type: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    with _conn(path) as c:
        row = c.execute(
            "SELECT * FROM downloads WHERE id=? AND media_type=?", (str(item_id), media_type)
        ).fetchone()
    return dict(row) if row else None


def is_downloaded(path: str, item_id: Any, media_type: str, quality: int = 0) -> Optional[str]:
    """Devolve o ``saved_path`` se já baixado em qualidade >= ``quality``.

    Registro com caminho inexistente é removido (e devolve None).
    """
    rec = get_record(path, item_id, media_type)
    if rec is None:
        return None
    saved = rec.get("saved_path") or ""
    if saved and not os.path.exists(saved):
        remove_record(path, item_id, media_type)
        return None
    if int(rec.get("quality") or 0) < int(quality):
        return None
    return saved or ""


def mark_downloaded(
    path: str,
    item_id: Any,
    media_type: str,
    *,
    quality: int,
    saved_path: str = "",
    file_format: str = "",
    bit_depth: Optional[int] = None,
    sampling_rate: Optional[float] = None,
    artist: str = "",
    album: str = "",
    title: str = "",
    release_date: str = "",
) -> None:
    init_db(path)
    with _conn(path) as c:
        c.execute(
            "INSERT OR REPLACE INTO downloads (id, media_type, quality, file_format, bit_depth, "
            "sampling_rate, saved_path, artist, album, title, release_date, downloaded_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(item_id), media_type, int(quality), file_format, bit_depth, sampling_rate,
             saved_path, artist, album, title, release_date,
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )


def remove_record(path: str, item_id: Any, media_type: str) -> None:
    if not os.path.isfile(path):
        return
    with _conn(path) as c:
        c.execute("DELETE FROM downloads WHERE id=? AND media_type=?", (str(item_id), media_type))


def purge(path: str) -> int:
    if not os.path.isfile(path):
        return 0
    with _conn(path) as c:
        return c.execute("DELETE FROM downloads").rowcount


def get_stats(path: str) -> dict:
    """Totais para o comando ``stats``."""
    empty = {"total": 0, "by_type": {}, "by_format": {}, "by_year": {}, "latest": []}
    if not os.path.isfile(path):
        return empty
    try:
        with _conn(path, readonly=True) as c:
            total = c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            by_type = {r[0]: r[1] for r in c.execute(
                "SELECT media_type, COUNT(*) FROM downloads GROUP BY media_type")}
            by_format = {
                (r[0] or "?"): r[1]
                for r in c.execute("SELECT file_format, COUNT(*) FROM downloads "
                                   "WHERE media_type='track' GROUP BY file_format ORDER BY 2 DESC")
            }
            by_year = {
                r[0]: r[1] for r in c.execute(
                    "SELECT substr(release_date,1,4) y, COUNT(*) FROM downloads "
                    "WHERE media_type='album' AND release_date != '' GROUP BY y ORDER BY y DESC LIMIT 10")
            }
            latest = [dict(r) for r in c.execute(
                "SELECT artist, album, title, media_type, downloaded_at FROM downloads "
                "ORDER BY downloaded_at DESC LIMIT 5")]
    except sqlite3.Error:
        return empty
    return {"total": total, "by_type": by_type, "by_format": by_format,
            "by_year": by_year, "latest": latest}


# -- wrappers assíncronos ----------------------------------------------------


async def a_is_downloaded(path: str, item_id: Any, media_type: str, quality: int = 0):
    return await asyncio.to_thread(is_downloaded, path, item_id, media_type, quality)


async def a_mark_downloaded(path: str, item_id: Any, media_type: str, **kw: Any) -> None:
    await asyncio.to_thread(lambda: mark_downloaded(path, item_id, media_type, **kw))
