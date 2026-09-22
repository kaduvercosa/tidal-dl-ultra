"""Orquestração: login -> cliente -> URLs/busca -> downloader.

Espelha o papel do core.py do qobuz-dl-ultra (classe principal que a CLI
instancia), mas bem menor: o Tidal não exige raspar segredos de bundle.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from tidal_dl import ui
from tidal_dl.interactive_ui import _align_text, _get_table_layout, _shade, pt_style, prompt_style
from tidal_dl.api import TidalAPI
from tidal_dl.auth import CredentialStore, Credentials
from tidal_dl.downloader import Downloader
from tidal_dl.exceptions import (
    AuthenticationError,
    NonStreamable,
    ResourceNotFoundError,
    TidalDLException,
)
from tidal_dl.models import Album
from tidal_dl.net import HttpClient, create_client
from tidal_dl.settings import TidalDLSettings
from tidal_dl.utils import get_config_paths, human_duration, parse_url

logger = logging.getLogger(__name__)

SEARCH_KINDS = {"album": "albums", "track": "tracks", "artist": "artists", "playlist": "playlists"}


class TidalDL:
    def __init__(
        self,
        settings: TidalDLSettings,
        *,
        paths: Optional[dict] = None,
        http: Optional[HttpClient] = None,
    ):
        self.settings = settings
        self.paths = paths or get_config_paths()
        self.store = CredentialStore(
            self.paths["credentials_file"], use_keyring=not settings.disable_keyring
        )
        self._http = http
        self.api: Optional[TidalAPI] = None
        self.downloader: Optional[Downloader] = None

    @property
    def downloads_db(self) -> Optional[str]:
        return None if self.settings.no_database else self.paths["tidal_db"]

    # -- ciclo de vida -----------------------------------------------------

    def _persist(self, creds: Credentials) -> None:
        try:
            self.store.save(creds)
        except OSError as exc:
            ui.warn(f"não foi possível salvar o token renovado: {exc}")

    async def initialize(self) -> "TidalDL":
        creds = self.store.load()
        if creds is None:
            raise AuthenticationError("Você não está logado. Rode: tidal-dl login")
        if self._http is None:
            self._http = create_client(
                requests_per_minute=self.settings.requests_per_minute, retries=self.settings.retries
            )
        self.api = TidalAPI(self._http, creds, on_refresh=self._persist)
        await self.api.ensure_token()
        self.downloader = Downloader(self.api, self.settings, db_path=self.downloads_db)
        return self

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # -- downloads ---------------------------------------------------------

    def _need(self) -> tuple[TidalAPI, Downloader]:
        if self.api is None or self.downloader is None:
            raise TidalDLException("TidalDL não inicializado (chame initialize()).")
        return self.api, self.downloader

    async def download_from_id(self, item_id: Any, kind: str = "album", *, include_eps: bool = False) -> bool:
        """Baixa um item. Devolve True se tudo deu certo (ou já existia)."""
        api, dl = self._need()
        kind = kind.lower()
        try:
            if kind == "album":
                return (await dl.download_album(item_id)).ok
            if kind == "track":
                return (await dl.download_track(item_id)).ok
            if kind == "playlist":
                return (await dl.download_playlist(str(item_id))).ok
            if kind == "video":
                return (await dl.download_video(item_id)).ok
            if kind == "artist":
                return await self._download_artist(item_id, include_eps)
        except ResourceNotFoundError:
            ui.error(f"{kind} {item_id} não encontrado.")
            return False
        except NonStreamable as exc:
            ui.error(f"Indisponível: {exc}")
            return False
        raise TidalDLException(f"tipo de item desconhecido: {kind}")

    async def _download_artist(self, artist_id: Any, include_eps: bool) -> bool:
        api, dl = self._need()
        info = await api.get_artist(artist_id)
        albums = await api.get_artist_albums(artist_id)
        if include_eps:
            extra = await api.get_artist_albums(artist_id, eps_and_singles=True)
            seen = {a.id for a in albums}
            albums += [a for a in extra if a.id not in seen]
        albums.sort(key=lambda a: (a.release_date or "9999", a.title))
        ui.info(f"{info.get('name', artist_id)}: {len(albums)} álbum(ns).")
        ok = True
        for a in albums:
            ok = (await dl.download_album(a.id)).ok and ok
        return ok

    async def handle_url(self, url: str, *, include_eps: bool = False) -> bool:
        parsed = parse_url(url)
        if not parsed:
            ui.error(f"URL não reconhecida: {url}")
            return False
        kind, ident = parsed
        return await self.download_from_id(ident, kind, include_eps=include_eps)

    async def download_urls(self, urls: list[str], *, include_eps: bool = False) -> dict:
        ok = failed = 0
        for u in urls:
            try:
                good = await self.handle_url(u, include_eps=include_eps)
            except AuthenticationError:
                raise
            except TidalDLException as exc:
                ui.error(f"{u}: {exc}")
                good = False
            ok, failed = (ok + 1, failed) if good else (ok, failed + 1)
        return {"ok": ok, "failed": failed}

    # -- busca -------------------------------------------------------------

    async def search(self, kind: str, query: str, limit: int = 15) -> list[dict]:
        api, _ = self._need()
        page = await api.search(SEARCH_KINDS[kind], query, limit=limit)
        return page.items

    @staticmethod
    def describe(kind: str, item: dict) -> tuple[str, str]:
        """``(id, texto)`` de um resultado de busca."""
        if kind == "album":
            a = Album.from_dict(item)
            return str(a.id), f"{a.album_artist} - {a.full_title} ({a.year}) [{a.audio_quality or '?'}]"
        if kind == "track":
            art = (item.get("artist") or {}).get("name", "?")
            alb = (item.get("album") or {}).get("title", "")
            return str(item.get("id")), f"{art} - {item.get('title')} · {alb} ({human_duration(item.get('duration'))})"
        if kind == "artist":
            return str(item.get("id")), str(item.get("name", "?"))
        return str(item.get("uuid")), f"{item.get('title')} ({item.get('numberOfTracks', '?')} faixas)"

    async def lucky(self, query: str, kind: str = "album", number: int = 1) -> bool:
        items = await self.search(kind, query, limit=max(number, 1))
        if not items:
            ui.warn(f"Nada encontrado para: {query}")
            return False
        ok = True
        for it in items[:number]:
            ident, text = self.describe(kind, it)
            ui.info(f"Sorte: {text}")
            ok = await self.download_from_id(ident, kind) and ok
        return ok

    async def interactive(self, query: str, kind: str = "album", ask=None) -> bool:
        """Busca e deixa escolher por número (``1,3-5``; ``q`` sai). ``ask`` é injetável."""
        items = await self.search(kind, query, limit=20)
        if not items:
            ui.warn(f"Nada encontrado para: {query}")
            return False
        rows = [self.describe(kind, it) for it in items]
        ui.section(f"RESULTADOS ({kind})")
        for i, (_id, text) in enumerate(rows, start=1):
            ui.emit_always(f" {i:>2}. {ui.truncate(text, max(20, ui.width() - 6))}")
        ask = ask or (lambda: asyncio.to_thread(input, "\nNúmeros (ex.: 1,3-5) ou q: "))
        picks = parse_selection((await ask()).strip(), len(rows))
        if not picks:
            ui.info("Nada selecionado.")
            return False
        ok = True
        for n in picks:
            ok = await self.download_from_id(rows[n - 1][0], kind) and ok
        return ok


def parse_selection(text: str, maximum: int) -> list[int]:
    """``"1,3-5"`` -> ``[1, 3, 4, 5]`` (só valores em 1..maximum, sem repetir)."""
    if not text or text.lower() in ("q", "quit", "sair"):
        return []
    picked: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                rng = range(min(a, b), max(a, b) + 1)
            else:
                rng = range(int(part), int(part) + 1)
        except ValueError:
            continue
        for n in rng:
            if 1 <= n <= maximum and n not in picked:
                picked.append(n)
    return picked


def read_url_file(path: str) -> list[str]:
    """Lê URLs de um .txt (uma por linha; ``#`` comenta)."""
    urls = []
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                urls.append(line)
    return urls
