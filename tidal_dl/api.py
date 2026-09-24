"""Cliente da API v1 do Tidal (catálogo, favoritos, playlists, letras, playback).

Usa a API v1 legada (api.tidalhifi.com/v1) porque é a única que expõe
``playbackinfopostpaywall``. Toda chamada:
  * leva ``countryCode`` e ``Authorization: Bearer``;
  * renova o token sozinha uma vez no 401 e repete;
  * converte erro HTTP em exceção do projeto (net.raise_for_status).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from tidal_dl import auth as auth_mod
from tidal_dl.constants import API_URL, LISTEN_URL
from tidal_dl.exceptions import AuthenticationError, NonStreamable, ResourceNotFoundError
from tidal_dl.models import Album, Page, Playlist, Track, Video
from tidal_dl.net import HttpClient, raise_for_status

logger = logging.getLogger(__name__)

PAGE = 100  # máximo por página da API v1 para itens de álbum/playlist


class TidalAPI:
    def __init__(
        self,
        http: HttpClient,
        creds: auth_mod.Credentials,
        *,
        on_refresh: Optional[Callable[[auth_mod.Credentials], None]] = None,
    ):
        self.http = http
        self.creds = creds
        self._on_refresh = on_refresh
        self._refresh_lock: Optional[asyncio.Lock] = None

    # -- token -------------------------------------------------------------

    @property
    def country(self) -> str:
        return self.creds.country_code or "US"

    @property
    def user_id(self) -> str:
        return self.creds.user_id

    async def refresh(self) -> None:
        """Renova o token (serializado: várias tarefas em 401 fazem UM refresh)."""
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        before = self.creds.access_token
        async with self._refresh_lock:
            if self.creds.access_token != before:
                return  # outra tarefa já renovou
            self.creds = await auth_mod.refresh_credentials(self.http, self.creds)
            if self._on_refresh:
                self._on_refresh(self.creds)

    async def ensure_token(self, window: float = 300.0) -> bool:
        """Renova se o token expira em breve. True se renovou."""
        if not self.creds.is_stale(window):
            return False
        try:
            await self.refresh()
            return True
        except AuthenticationError:
            if self.creds.expires_in() <= 0:
                raise
            return False  # ainda válido: deixa o 401 real decidir

    # -- transporte --------------------------------------------------------

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.creds.access_token}"}

    async def _send(self, method: str, url: str, params: Optional[dict], data: Optional[dict]):
        merged = {"countryCode": self.country}
        if params:
            merged.update(params)
        return await self.http.request(method, url, params=merged, data=data, headers=self._headers())

    async def _call(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        *,
        data: Optional[dict] = None,
        base: str = API_URL,
        allow: tuple[int, ...] = (),
    ) -> dict:
        url = f"{base}/{endpoint.lstrip('/')}"
        resp = await self._send(method, url, params, data)
        if resp.status == 401 and self.creds.refresh_token:
            try:
                await self.refresh()
            except AuthenticationError:
                raise_for_status(resp)
            resp = await self._send(method, url, params, data)
        if resp.status not in allow:
            raise_for_status(resp)
        body = resp.json()
        return body if isinstance(body, dict) else {}

    async def get(self, endpoint: str, params: Optional[dict] = None, *, base: str = API_URL) -> dict:
        return await self._call("GET", endpoint, params, base=base)

    # -- conta -------------------------------------------------------------

    async def session_info(self) -> dict:
        return await self.get("sessions")

    async def user_info(self) -> dict:
        return await self.get(f"users/{self.user_id}")

    async def subscription(self) -> dict:
        return await self.get(f"users/{self.user_id}/subscription")

    # -- catálogo ----------------------------------------------------------

    async def get_album(self, album_id: Any) -> Album:
        return Album.from_dict(await self.get(f"albums/{album_id}"))

    async def _paged_items(self, endpoint: str, *, extra: Optional[dict] = None) -> list[dict]:
        """Junta todas as páginas de ``.../items`` (desembrulha ``{item, type}``)."""
        return [d for t, d in await self._paged_items_typed(endpoint, extra=extra) if t == "track"]

    async def _paged_items_typed(
        self, endpoint: str, *, extra: Optional[dict] = None
    ) -> list[tuple[str, dict]]:
        """Como ``_paged_items``, mas preserva o ``type`` de cada entrada
        (``"track"``/``"video"``) em vez de descartar tudo que não é faixa.

        Usado por ``get_album_items`` para não perder mais vídeos embutidos
        num álbum (deluxe editions costumam ter 1-2 videoclipes junto das
        faixas) -- ver comentário em ``get_album_items``.
        """
        out: list[tuple[str, dict]] = []
        offset = 0
        while True:
            params = {"limit": PAGE, "offset": offset, **(extra or {})}
            body = await self.get(endpoint, params)
            items = body.get("items") or []
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                etype = entry.get("type") or "track"
                if etype not in ("track", "video"):
                    continue
                out.append((etype, entry.get("item", entry)))
            total = int(body.get("totalNumberOfItems") or 0)
            if not items or len(items) < PAGE or (total and offset + len(items) >= total):
                break
            offset += len(items)
        return out

    async def get_album_tracks(self, album_id: Any) -> list[Track]:
        return [Track.from_dict(i) for i in await self._paged_items(f"albums/{album_id}/items")]

    async def get_album_items(self, album_id: Any) -> tuple[list[Track], list[Video]]:
        """``(faixas, vídeos)`` de um álbum.

        ANTES: ``_paged_items`` descartava silenciosamente qualquer entrada
        com ``type != "track"`` ("# vídeos etc." no comentário original) --
        álbuns/deluxe editions que trazem um videoclipe junto das faixas
        (comum em lançamentos "deluxe: ... visualizer" etc.) tinham esse
        vídeo simplesmente ignorado: não contava nas faixas do álbum, não
        baixava com o resto. Agora os dois tipos são preservados numa única
        passada pela paginação (não busca a lista 2x).
        """
        entries = await self._paged_items_typed(f"albums/{album_id}/items")
        tracks = [Track.from_dict(d) for t, d in entries if t == "track"]
        videos = [Video.from_dict(d) for t, d in entries if t == "video"]
        return tracks, videos

    async def get_album_with_tracks(self, album_id: Any) -> tuple[Album, list[Track]]:
        album, tracks, _videos = await self.get_album_with_items(album_id)
        return album, tracks

    async def get_album_with_items(self, album_id: Any) -> tuple[Album, list[Track], list[Video]]:
        album, (tracks, videos) = await asyncio.gather(
            self.get_album(album_id), self.get_album_items(album_id)
        )
        return album, tracks, videos

    async def get_track(self, track_id: Any) -> Track:
        return Track.from_dict(await self.get(f"tracks/{track_id}"))

    async def get_playlist(self, uuid: str) -> Playlist:
        return Playlist.from_dict(await self.get(f"playlists/{uuid}"))

    async def get_playlist_tracks(self, uuid: str) -> list[Track]:
        return [Track.from_dict(i) for i in await self._paged_items(f"playlists/{uuid}/items")]

    async def get_artist(self, artist_id: Any) -> dict:
        return await self.get(f"artists/{artist_id}")

    async def get_artist_albums(self, artist_id: Any, *, eps_and_singles: bool = False) -> list[Album]:
        albums: list[Album] = []
        offset = 0
        while True:
            params: dict[str, Any] = {"limit": PAGE, "offset": offset}
            if eps_and_singles:
                params["filter"] = "EPSANDSINGLES"
            page = Page.from_dict(await self.get(f"artists/{artist_id}/albums", params))
            albums.extend(Album.from_dict(i) for i in page.items)
            if not page.items or not page.has_more:
                break
            offset += len(page.items)
        return albums

    async def search(self, kind: str, query: str, *, limit: int = 25, offset: int = 0) -> Page:
        """``kind``: albums | tracks | artists | playlists."""
        body = await self.get(f"search/{kind}", {"query": query, "limit": limit, "offset": offset})
        if isinstance(body.get(kind), dict):  # alguns retornos vêm aninhados
            body = body[kind]
        return Page.from_dict(body)

    # -- favoritos ---------------------------------------------------------

    async def favorite_albums_page(self, *, limit: int = 100, offset: int = 0) -> Page:
        body = await self.get(
            f"users/{self.user_id}/favorites/albums",
            {"limit": limit, "offset": offset, "order": "DATE", "orderDirection": "DESC"},
        )
        return Page.from_dict(body)

    async def favorite_tracks_page(self, *, limit: int = 100, offset: int = 0) -> Page:
        return Page.from_dict(
            await self.get(f"users/{self.user_id}/favorites/tracks", {"limit": limit, "offset": offset})
        )

    async def favorite_artists_page(self, *, limit: int = 100, offset: int = 0) -> Page:
        return Page.from_dict(
            await self.get(
                f"users/{self.user_id}/favorites/artists",
                {"limit": limit, "offset": offset, "order": "DATE", "orderDirection": "DESC"},
            )
        )

    async def favorite_playlists_page(self, *, limit: int = 100, offset: int = 0) -> Page:
        return Page.from_dict(
            await self.get(
                f"users/{self.user_id}/favorites/playlists",
                {"limit": limit, "offset": offset, "order": "DATE", "orderDirection": "DESC"},
            )
        )

    # -- letras / playback -------------------------------------------------

    async def get_lyrics(self, track_id: Any) -> dict:
        """Letras (``lyrics`` texto + ``subtitles`` LRC). ``{}`` se não houver."""
        try:
            return await self._call("GET", f"tracks/{track_id}/lyrics", base=LISTEN_URL)
        except (ResourceNotFoundError, AuthenticationError):
            return {}
        except Exception as exc:  # letra nunca pode derrubar o download
            logger.debug("letra indisponível para %s: %s", track_id, exc)
            return {}

    async def playback_info(self, track_id: Any, audio_quality: str) -> dict:
        body = await self.get(
            f"tracks/{track_id}/playbackinfopostpaywall",
            {"audioquality": audio_quality, "playbackmode": "STREAM", "assetpresentation": "FULL"},
        )
        if "manifest" not in body:
            raise NonStreamable(str(body.get("userMessage") or "resposta sem manifest"))
        return body

    # -- vídeos --------------------------------------------------------

    async def get_video(self, video_id: Any) -> Video:
        return Video.from_dict(await self.get(f"videos/{video_id}"))

    async def video_playback_info(self, video_id: Any, video_quality: str = "HIGH") -> dict:
        """``video_quality``: ``LOW`` | ``MEDIUM`` | ``HIGH`` (enum da API, não resolução)."""
        body = await self.get(
            f"videos/{video_id}/playbackinfopostpaywall",
            {"videoquality": video_quality, "playbackmode": "STREAM", "assetpresentation": "FULL"},
        )
        if "manifest" not in body:
            raise NonStreamable(str(body.get("userMessage") or "resposta de vídeo sem manifest"))
        return body
