"""Letras do Tidal e provedores de fallback.

O fluxo de fallback é deliberadamente pequeno e explícito:

    Tidal -> Musixmatch -> LRCLIB

O Musixmatch não exige uma chave fornecida pelo usuário, mas entrega um token
de sessão temporário pelo endpoint mobile. Ele fica somente em memória. A API
também mudou o formato da resposta algumas vezes; por isso o parser aceita
LRC pronto e JSON de linhas sincronizadas.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

LRCLIB_URL = "https://lrclib.net/api/get"
MXM_TOKEN_URL = "https://apic-appmobile.musixmatch.com/ws/1.1/token.get"
MXM_SUBTITLES_URL = "https://apic-appmobile.musixmatch.com/ws/1.1/macro.subtitles.get"
MXM_HEADERS = {
    "x-mxm-app-version": "10.1.1",
    "User-Agent": "Musixmatch/2025120901 CFNetwork/1404.0.5 Darwin/22.3.0",
}

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


def _timestamp(seconds: Any) -> str:
    """Converte segundos/milisegundos do richsync em uma marca LRC."""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    # Algumas versões retornam segundos; outras, milissegundos.
    if value > 10000:
        value /= 1000
    minutes, remainder = divmod(max(0.0, value), 60)
    return f"[{int(minutes):02d}:{remainder:06.3f}]"


def _musixmatch_body_to_text(body: Any) -> tuple[str, str]:
    """Normaliza ``subtitle_body`` para ``(texto, lrc)``.

    O endpoint mobile já retornou tanto uma string LRC quanto uma string JSON
    com objetos ``text``/``time``. Não aceitar uma resposta desconhecida como
    letra evita embutir JSON bruto no arquivo de áudio.
    """
    if not isinstance(body, str):
        return "", ""
    raw = body.strip()
    if not raw:
        return "", ""

    if any(_LRC_LINE.match(line.strip()) for line in raw.splitlines()):
        lrc = raw
        plain = "\n".join(
            re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
            for line in lrc.splitlines()
            if re.sub(r"^\[[^\]]*\]\s*", "", line).strip()
        )
        return plain, lrc

    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        # Pode ser uma letra simples não sincronizada. Só aceitamos texto
        # humano, não uma resposta de erro/objeto serializado.
        if raw.startswith(("{", "[", "<")):
            return "", ""
        return raw, ""

    if not isinstance(rows, list):
        return "", ""
    plain_rows: list[str] = []
    lrc_rows: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or row.get("line") or "").strip()
        if not text:
            continue
        plain_rows.append(text)
        time = row.get("time")
        if isinstance(time, dict):
            time = time.get("total") or time.get("seconds") or time.get("start")
        stamp = _timestamp(time)
        lrc_rows.append(f"{stamp} {text}" if stamp else text)
    if not plain_rows:
        return "", ""
    lrc = "\n".join(lrc_rows) if all(row.startswith("[") for row in lrc_rows) else ""
    return "\n".join(plain_rows), lrc


class MusixmatchClient:
    """Cliente assíncrono do endpoint mobile público do Musixmatch.

    O token é apenas um token de sessão temporário. Nunca é salvo em disco,
    nunca é exibido e é protegido por um lock para downloads paralelos não
    dispararem uma requisição de token por faixa.
    """

    def __init__(self, http: Any):
        self.http = http
        self._token: Optional[str] = None
        self._token_lock = asyncio.Lock()
        self._disabled = False

    async def _get_token(self) -> Optional[str]:
        if self._token or self._disabled:
            return self._token
        async with self._token_lock:
            if self._token or self._disabled:
                return self._token
            try:
                response = await self.http.request(
                    "GET", MXM_TOKEN_URL, params={"app_id": "mac-ios-v2.0"},
                    headers=MXM_HEADERS, timeout=8, retry=False,
                )
                data = response.json()
                header = (data.get("message") or {}).get("header") or {}
                token = ((data.get("message") or {}).get("body") or {}).get("user_token")
                if response.status == 200 and header.get("status_code") == 200 and token:
                    self._token = str(token)
                elif response.status in (401, 403) or header.get("status_code") in (401, 403):
                    self._disabled = True
            except Exception as exc:  # letras não podem derrubar o download
                logger.debug("Musixmatch token indisponível: %s", exc)
            return self._token

    async def fetch(self, artist: str, title: str) -> dict:
        token = await self._get_token()
        if not token:
            return {}
        params = {
            "q_artist": artist,
            "q_track": title,
            "format": "json",
            "namespace": "lyrics_richsynched",
            "usertoken": token,
            "app_id": "mac-ios-v2.0",
        }
        try:
            response = await self.http.request(
                "GET", MXM_SUBTITLES_URL, params=params,
                headers=MXM_HEADERS, timeout=8, retry=False,
            )
            data = response.json()
            message = data.get("message") or {}
            header = message.get("header") or {}
            if response.status != 200 or header.get("status_code") != 200:
                # 401/captcha e respostas semelhantes não devem ser tentadas
                # para cada faixa da mesma execução.
                if response.status in (401, 403) or header.get("status_code") in (401, 403):
                    self._token = None
                    self._disabled = True
                return {}
            body = message.get("body") or {}
            macro = body.get("macro_calls") or {}
            subtitle_call = macro.get("track.subtitles.get") or {}
            subtitle_message = subtitle_call.get("message") or {}
            subtitles = (subtitle_message.get("body") or {}).get("subtitle_list") or []
            if not subtitles:
                return {}
            subtitle = (subtitles[0] or {}).get("subtitle") or {}
            plain, lrc = _musixmatch_body_to_text(subtitle.get("subtitle_body"))
            if not plain and not lrc:
                return {}
            return {"lyrics": plain, "subtitles": lrc, "_source": "Musixmatch"}
        except Exception as exc:  # provider opcional
            logger.debug("Musixmatch indisponível para %s - %s: %s", title, exc)
            return {}


async def fetch_musixmatch_lyrics(http: Any, artist: str, title: str, client: Any = None) -> dict:
    """Função conveniente/testável para buscar uma letra no Musixmatch."""
    return await (client or MusixmatchClient(http)).fetch(artist, title)


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
    plain = data.get("plainLyrics") or ""
    synced = data.get("syncedLyrics") or ""
    if not plain and not synced:
        return {}
    return {"lyrics": plain, "subtitles": synced}
