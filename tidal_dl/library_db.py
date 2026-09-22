"""Catálogo local da biblioteca (``library.db``): favoritos + estado de download.

ORIGEM DA IDEIA
---------------
Portado do modelo de dados do libsync (``backend/models/database.py``): uma
tabela ``albums`` com o que existe no serviço (favoritos) e o estado local de
cada álbum, mais ``sync_runs`` (histórico de sincronizações). É a base dos
comandos ``sync-favorites``, ``scan`` e ``library``.

POR QUE UM BANCO SEPARADO DO ``tidal_dl.db``?
---------------------------------------------
O ``tidal_dl.db`` (tabela ``downloads``) é o dedup por (id, qualidade) do
downloader e tem formato/contrato próprio (com migrações e testes). Este banco
responde a outra pergunta -- "o que eu TENHO na conta vs. o que eu TENHO no
disco?" -- e pode ser apagado a qualquer momento sem perder nada além do cache
(é reconstruído por ``sync-favorites`` + ``scan``).

Decisões herdadas de bugs reais do libsync:
  * ``upsert_album`` é um UPSERT *estreito*: campos omitidos (None) NUNCA
    sobrescrevem um valor já gravado. (No libsync, um upsert ingênuo apagava a
    capa de todo álbum assim que o download terminava.)
  * Estados "em voo" (``queued``/``downloading``) não sobrevivem a um
    reinício -- ``reset_stuck`` os devolve a ``not_downloaded``.

Só usa ``sqlite3`` da biblioteca padrão (uma conexão curta por operação, então
é seguro chamar via ``asyncio.to_thread``).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

SCHEMA_VERSION = 1

# Estados possíveis de ``albums.download_status``.
STATUS_NOT_DOWNLOADED = "not_downloaded"
STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_COMPLETE = "complete"
STATUS_INCOMPLETE = "incomplete"
STATUS_FAILED = "failed"
IN_FLIGHT_STATUSES = (STATUS_QUEUED, STATUS_DOWNLOADING)

_ALBUM_COLUMNS = (
    "title",
    "artist",
    "track_count",
    "bit_depth",
    "sample_rate",
    "release_date",
    "label",
    "genre",
    "upc",
    "duration_seconds",
    "cover_url",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AlbumNotFoundError(LookupError):
    """Álbum inexistente no catálogo local."""


class LibraryDB:
    """Acesso ao ``library.db``. Uma conexão curta por chamada."""

    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    # -- infraestrutura ----------------------------------------------------

    @contextlib.contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit/rollback automático
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version >= SCHEMA_VERSION:
                return
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS albums (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    source_album_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    track_count INTEGER,
                    bit_depth INTEGER,
                    sample_rate REAL,
                    release_date TEXT,
                    label TEXT,
                    genre TEXT,
                    upc TEXT,
                    duration_seconds INTEGER,
                    cover_url TEXT,
                    download_status TEXT NOT NULL DEFAULT 'not_downloaded',
                    downloaded_at TEXT,
                    local_folder_path TEXT,
                    removed_from_service INTEGER NOT NULL DEFAULT 0,
                    added_to_library_at TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    UNIQUE (source, source_album_id)
                );
                CREATE INDEX IF NOT EXISTS idx_albums_status
                    ON albums (source, download_status);
                CREATE TABLE IF NOT EXISTS sync_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    albums_found INTEGER NOT NULL DEFAULT 0,
                    albums_new INTEGER NOT NULL DEFAULT 0,
                    albums_removed INTEGER NOT NULL DEFAULT 0,
                    albums_downloaded INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # -- álbuns ------------------------------------------------------------

    def upsert_album(
        self,
        source: str,
        source_album_id: Any,
        title: str,
        artist: str,
        **fields: Any,
    ) -> int:
        """Insere/atualiza um álbum e devolve o ``id`` local.

        UPSERT *estreito*: só grava campos NÃO-None e nunca mexe em
        ``download_status``/``local_folder_path``/``downloaded_at`` de um álbum
        já existente. Reaparecer na conta zera ``removed_from_service``.
        """
        source_album_id = str(source_album_id)
        now = _now()
        extras = {k: v for k, v in fields.items() if k in _ALBUM_COLUMNS and v is not None}
        added_at = fields.get("added_to_library_at")
        with self._conn() as c:
            row = c.execute(
                "SELECT id FROM albums WHERE source=? AND source_album_id=?",
                (source, source_album_id),
            ).fetchone()
            if row is None:
                cols = ["source", "source_album_id", "title", "artist",
                        "first_seen_at", "last_seen_at", "added_to_library_at"]
                vals: list[Any] = [source, source_album_id, title or "", artist or "",
                                   now, now, added_at or now]
                for k, v in extras.items():
                    if k in ("title", "artist"):
                        continue
                    cols.append(k)
                    vals.append(v)
                marks = ",".join("?" for _ in cols)
                cur = c.execute(
                    f"INSERT INTO albums ({','.join(cols)}) VALUES ({marks})", vals
                )
                return int(cur.lastrowid)
            sets = ["last_seen_at=?", "removed_from_service=0"]
            vals = [now]
            if title:
                sets.append("title=?")
                vals.append(title)
            if artist:
                sets.append("artist=?")
                vals.append(artist)
            for k, v in extras.items():
                if k in ("title", "artist"):
                    continue
                sets.append(f"{k}=?")
                vals.append(v)
            vals.append(row["id"])
            c.execute(f"UPDATE albums SET {', '.join(sets)} WHERE id=?", vals)
            return int(row["id"])

    def get_album(self, album_id: int) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM albums WHERE id=?", (album_id,)).fetchone()
        return dict(row) if row else None

    def get_album_by_source_id(self, source: str, source_album_id: Any) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM albums WHERE source=? AND source_album_id=?",
                (source, str(source_album_id)),
            ).fetchone()
        return dict(row) if row else None

    def get_albums(
        self,
        source: Optional[str] = None,
        status: Optional[str] = None,
        *,
        include_removed: bool = True,
        limit: Optional[int] = None,
    ) -> list[dict]:
        sql = "SELECT * FROM albums WHERE 1=1"
        args: list[Any] = []
        if source:
            sql += " AND source=?"
            args.append(source)
        if status:
            sql += " AND download_status=?"
            args.append(status)
        if not include_removed:
            sql += " AND removed_from_service=0"
        sql += " ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE"
        if limit:
            sql += " LIMIT ?"
            args.append(int(limit))
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def get_all_albums_for_index(self, source: Optional[str] = None) -> list[dict]:
        """Linhas leves usadas para montar o índice de matching do ``scan``."""
        return self.get_albums(source, include_removed=True)

    # -- estado de download ------------------------------------------------

    def update_status(self, album_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE albums SET download_status=? WHERE id=?", (status, album_id))

    def set_download_state(
        self,
        album_id: int,
        *,
        downloaded: bool,
        downloaded_at: Optional[str] = None,
        local_folder_path: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """Marca (ou desmarca) um álbum como baixado. Devolve a linha anterior.

        ``local_folder_path=None`` preserva o caminho já gravado ao MARCAR; ao
        desmarcar, o caminho é limpo.
        """
        old = self.get_album(album_id)
        if old is None:
            raise AlbumNotFoundError(f"Álbum {album_id} não encontrado")
        with self._conn() as c:
            if downloaded:
                c.execute(
                    "UPDATE albums SET download_status=?, downloaded_at=?, "
                    "local_folder_path=COALESCE(?, local_folder_path) WHERE id=?",
                    (status or STATUS_COMPLETE, downloaded_at or _now(),
                     local_folder_path, album_id),
                )
            else:
                c.execute(
                    "UPDATE albums SET download_status=?, downloaded_at=NULL, "
                    "local_folder_path=NULL WHERE id=?",
                    (status or STATUS_NOT_DOWNLOADED, album_id),
                )
        return old

    def set_local_folder(self, album_id: int, path: Optional[str]) -> None:
        """Grava só o caminho local (sem mexer no status)."""
        with self._conn() as c:
            c.execute("UPDATE albums SET local_folder_path=? WHERE id=?", (path, album_id))

    def reset_stuck(self, source: Optional[str] = None) -> int:
        """Devolve ``queued``/``downloading`` a ``not_downloaded`` (artefato de reinício)."""
        sql = "UPDATE albums SET download_status=? WHERE download_status IN (?, ?)"
        args: list[Any] = [STATUS_NOT_DOWNLOADED, *IN_FLIGHT_STATUSES]
        if source:
            sql += " AND source=?"
            args.append(source)
        with self._conn() as c:
            return c.execute(sql, args).rowcount

    def mark_removed(self, source: str, present_ids: Iterable[Any]) -> list[dict]:
        """Marca como ``removed_from_service`` tudo que não está em ``present_ids``.

        Devolve as linhas *recém* marcadas (as que estavam ativas antes).
        """
        present = {str(i) for i in present_ids}
        removed: list[dict] = []
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM albums WHERE source=? AND removed_from_service=0",
                (source,),
            ).fetchall()
            for r in rows:
                if r["source_album_id"] not in present:
                    removed.append(dict(r))
                    c.execute(
                        "UPDATE albums SET removed_from_service=1 WHERE id=?", (r["id"],)
                    )
        return removed

    # -- relatórios --------------------------------------------------------

    def status_counts(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT source, download_status, COUNT(*) AS cnt FROM albums "
                "GROUP BY source, download_status ORDER BY source, download_status"
            ).fetchall()
        return [dict(r) for r in rows]

    def stuck_albums(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM albums WHERE download_status IN (?, ?) ORDER BY id",
                IN_FLIGHT_STATUSES,
            ).fetchall()
        return [dict(r) for r in rows]

    def complete_without_folder(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM albums WHERE download_status=? "
                "AND (local_folder_path IS NULL OR local_folder_path='')",
                (STATUS_COMPLETE,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -- histórico de sync -------------------------------------------------

    def create_sync_run(self, source: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO sync_runs (source, started_at) VALUES (?, ?)",
                (source, _now()),
            )
            return int(cur.lastrowid)

    def complete_sync_run(
        self,
        run_id: int,
        *,
        albums_found: int,
        albums_new: int,
        albums_removed: int = 0,
        albums_downloaded: int = 0,
    ) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sync_runs SET status='complete', completed_at=?, albums_found=?, "
                "albums_new=?, albums_removed=?, albums_downloaded=? WHERE id=?",
                (_now(), albums_found, albums_new, albums_removed, albums_downloaded, run_id),
            )

    def _finish_run(self, run_id: int, status: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sync_runs SET status=?, completed_at=? WHERE id=?",
                (status, _now(), run_id),
            )

    def fail_sync_run(self, run_id: int) -> None:
        self._finish_run(run_id, "failed")

    def interrupt_sync_run(self, run_id: int) -> None:
        self._finish_run(run_id, "interrupted")

    def get_sync_history(self, source: Optional[str] = None, limit: int = 10) -> list[dict]:
        sql = "SELECT * FROM sync_runs"
        args: list[Any] = []
        if source:
            sql += " WHERE source=?"
            args.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
