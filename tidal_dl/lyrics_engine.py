"""Lyrics Engine com suporte a Musixmatch (prioridade), Tidal API, LRCLIB e Genius.

Suporta busca, sincronização, salvamento em arquivo externo .lrc e injeção
em tags de áudio (FLAC/M4A/MP3).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx
from tidal_dl import __version__
from tidal_dl import ui
from tidal_dl.lyrics import plain_lyrics, synced_lyrics

logger = logging.getLogger(__name__)

try:
    import lyricsgenius
except ImportError:
    lyricsgenius = None


class LyricsEngine:
    """Responsável por buscar e aplicar letras de músicas."""

    def __init__(self, genius_token: Optional[str] = None, settings: Any = None):
        self.genius_token = genius_token
        self.genius = None
        self.settings = settings
        self._mxm_token: Optional[str] = None
        if self.genius_token and lyricsgenius:
            try:
                self.genius = lyricsgenius.Genius(self.genius_token, remove_section_headers=True)
                self.genius.verbose = False
            except Exception as exc:
                logger.debug("Falha ao inicializar Genius client: %s", exc)

    def _get_session(self) -> httpx.Client:
        return httpx.Client(timeout=10.0, follow_redirects=True)

    def fetch_musixmatch_lyrics(self, artist: str, title: str) -> Optional[str]:
        """Busca letras sincronizadas no Musixmatch (LRC)."""
        headers = {
            "x-mxm-app-version": "10.1.1",
            "User-Agent": "Musixmatch/2025120901 CFNetwork/1404.0.5 Darwin/22.3.0",
        }
        try:
            with self._get_session() as client:
                if not self._mxm_token:
                    tok_resp = client.get(
                        "https://apic-appmobile.musixmatch.com/ws/1.1/token.get?app_id=mac-ios-v2.0",
                        headers=headers,
                    )
                    if tok_resp.status_code == 200:
                        data = tok_resp.json()
                        msg = data.get("message", {})
                        if msg.get("header", {}).get("status_code") == 200:
                            self._mxm_token = msg.get("body", {}).get("user_token")

                if self._mxm_token:
                    params = {
                        "q_artist": artist,
                        "q_track": title,
                        "format": "json",
                        "namespace": "lyrics_richsynched",
                        "usertoken": self._mxm_token,
                        "app_id": "mac-ios-v2.0",
                    }
                    resp = client.get(
                        "https://apic-appmobile.musixmatch.com/ws/1.1/macro.subtitles.get",
                        params=params,
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        msg = data.get("message", {})
                        if msg.get("header", {}).get("status_code") == 200:
                            body = msg.get("body", {})
                            macro = body.get("macro_calls", {})
                            sub_get = macro.get("track.subtitles.get", {}).get("message", {})
                            if sub_get.get("header", {}).get("status_code") == 200:
                                sub_list = sub_get.get("body", {}).get("subtitle_list", [])
                                if sub_list and isinstance(sub_list, list):
                                    return sub_list[0].get("subtitle", {}).get("subtitle_body")
        except Exception as exc:
            logger.debug("Musixmatch fetch error: %s", exc)
        return None

    def fetch_lrclib_lyrics(self, artist: str, title: str, album: str) -> tuple[Optional[str], Optional[str]]:
        """Busca no LRCLIB. Retorna (synced_lyrics, plain_lyrics)."""
        headers = {
            "User-Agent": f"tidal-dl-ultra/{__version__} (https://github.com/kaduvercosa/tidal-dl-ultra)"
        }
        try:
            with self._get_session() as client:
                params = {"artist_name": artist, "track_name": title, "album_name": album}
                resp = client.get("https://lrclib.net/api/get", params=params, headers=headers)
                if resp.status_code != 200:
                    params = {"artist_name": artist, "track_name": title}
                    resp = client.get("https://lrclib.net/api/get", params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("syncedLyrics"), data.get("plainLyrics")
        except Exception as exc:
            logger.debug("LRCLIB fetch error: %s", exc)
        return None, None

    def _save_lrc_file(self, audio_path: str, content: str, source: str = "") -> bool:
        try:
            base = os.path.splitext(audio_path)[0]
            lrc_path = f"{base}.lrc"
            header = f"[by:{source}]\n" if source else ""
            with open(lrc_path, "w", encoding="utf-8") as fh:
                fh.write(header + content)
            return True
        except Exception as exc:
            logger.debug("Erro ao salvar .lrc: %s", exc)
            return False

    def _inject_metadata(self, audio_path: str, lyrics_text: str) -> bool:
        ext = os.path.splitext(audio_path)[1].lower()
        try:
            if ext == ".flac":
                from mutagen.flac import FLAC
                audio = FLAC(audio_path)
                audio["LYRICS"] = lyrics_text
                audio.save()
                return True
            if ext in (".m4a", ".mp4"):
                from mutagen.mp4 import MP4
                audio = MP4(audio_path)
                audio["\xa9lyr"] = [lyrics_text]
                audio.save()
                return True
            return False
        except Exception as exc:
            logger.debug("Injeção de metadados de letra falhou: %s", exc)
            return False

    def fetch_and_inject(
        self,
        file_path: str,
        artist: str,
        title: str,
        album: str = "",
        save_lrc: bool = True,
        embed_lyrics: bool = True,
        tidal_lyrics_resp: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Busca letra na ordem: Musixmatch -> Tidal API -> LRCLIB -> Genius e injeta/salva."""
        result = {"success": False, "source": None, "synchronized": False, "embedded": False, "saved_external": False}

        # 1. Musixmatch
        mxm = self.fetch_musixmatch_lyrics(artist, title)
        if mxm:
            result["source"] = "Musixmatch"
            result["synchronized"] = True
            if embed_lyrics:
                result["embedded"] = self._inject_metadata(file_path, mxm)
            if save_lrc:
                result["saved_external"] = self._save_lrc_file(file_path, mxm, "Musixmatch")
            result["success"] = result["embedded"] or result["saved_external"]
            ui.ok(f"  [LETRA] {title} (via Musixmatch)")
            return result

        # 2. Tidal API
        if tidal_lyrics_resp:
            sl = synced_lyrics(tidal_lyrics_resp)
            pl = plain_lyrics(tidal_lyrics_resp)
            content = sl or pl
            if content:
                result["source"] = "Tidal"
                result["synchronized"] = bool(sl)
                if embed_lyrics:
                    result["embedded"] = self._inject_metadata(file_path, content)
                if save_lrc and sl:
                    result["saved_external"] = self._save_lrc_file(file_path, sl, "Tidal")
                result["success"] = result["embedded"] or result["saved_external"]
                ui.ok(f"  [LETRA] {title} (via Tidal API)")
                return result

        # 3. LRCLIB
        sl, pl = self.fetch_lrclib_lyrics(artist, title, album)
        content = sl or pl
        if content:
            result["source"] = "LRCLIB"
            result["synchronized"] = bool(sl)
            if embed_lyrics:
                result["embedded"] = self._inject_metadata(file_path, content)
            if save_lrc and sl:
                result["saved_external"] = self._save_lrc_file(file_path, sl, "LRCLIB")
            result["success"] = result["embedded"] or result["saved_external"]
            ui.ok(f"  [LETRA] {title} (via LRCLIB)")
            return result

        # 4. Genius
        if self.genius:
            try:
                song = self.genius.search_song(title, artist)
                if song and song.lyrics:
                    result["source"] = "Genius"
                    result["synchronized"] = False
                    if embed_lyrics:
                        result["embedded"] = self._inject_metadata(file_path, song.lyrics)
                    if save_lrc:
                        result["saved_external"] = self._save_lrc_file(file_path, song.lyrics, "Genius")
                    result["success"] = result["embedded"] or result["saved_external"]
                    ui.ok(f"  [LETRA] {title} (via Genius)")
                    return result
            except Exception as exc:
                logger.debug("Genius search error: %s", exc)

        return result
