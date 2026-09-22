"""Camada HTTP do tidal-dl-ultra.

DESIGN
------
Todo o projeto fala com a rede por UMA interface pequena (``Backend``):

  * ``request()``  -> ``Response`` (status, headers, bytes, .json())
  * ``stream()``   -> context manager assíncrono que entrega chunks

A implementação real (``HttpxBackend``) usa ``httpx`` -- pacote Python puro,
que instala no a-Shell. Os testes injetam um ``FakeBackend`` e por isso a
lógica de API/downloader é testável sem rede e sem httpx instalado.

``HttpClient`` acrescenta o que toda chamada precisa: limite de requisições
por minuto, retry com backoff em erro de rede/5xx/429 (respeitando
``Retry-After``) e mapeamento de status para exceções do projeto.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional, Protocol

from tidal_dl.exceptions import (
    ApiError,
    AuthenticationError,
    ForbiddenError,
    RateLimitError,
    ResourceNotFoundError,
    TidalDLException,
)

logger = logging.getLogger(__name__)

USER_AGENT = "tidal-dl-ultra/0.1"


class NetworkError(TidalDLException):
    """Falha de transporte (DNS, conexão, timeout, TLS)."""


@dataclass
class Response:
    status: int
    headers: dict = field(default_factory=dict)
    content: bytes = b""

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return _json.loads(self.content.decode("utf-8")) if self.content else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def header(self, name: str, default: str = "") -> str:
        lname = name.lower()
        for k, v in self.headers.items():
            if k.lower() == lname:
                return str(v)
        return default


class StreamResponse(Protocol):
    status: int
    headers: dict

    def iter_chunks(self, size: int) -> AsyncIterator[bytes]: ...


class Backend(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Response: ...

    def stream(
        self, url: str, *, headers: Optional[dict] = None, timeout: Optional[float] = None
    ) -> "contextlib.AbstractAsyncContextManager[StreamResponse]": ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Backend real (httpx)
# ---------------------------------------------------------------------------


class HttpxBackend:
    """Backend baseado em ``httpx`` (import tardio: o módulo carrega sem ele)."""

    def __init__(self, *, timeout: float = 30.0, verify: bool = True):
        import httpx  # noqa: PLC0415 - import tardio de propósito

        self._httpx = httpx
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(timeout, connect=15.0),
            verify=verify,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def request(self, method, url, *, params=None, data=None, headers=None, timeout=None):
        try:
            r = await self._client.request(
                method, url, params=params, data=data, headers=headers,
                timeout=timeout if timeout is not None else httpx_default(self._httpx),
            )
        except self._httpx.HTTPError as exc:
            raise NetworkError(f"{type(exc).__name__}: {exc}") from exc
        return Response(r.status_code, dict(r.headers), r.content)

    @contextlib.asynccontextmanager
    async def stream(self, url, *, headers=None, timeout=None):
        try:
            async with self._client.stream(
                "GET", url, headers=headers,
                timeout=timeout if timeout is not None else httpx_default(self._httpx),
            ) as r:
                yield _HttpxStream(r, self._httpx)
        except self._httpx.HTTPError as exc:
            raise NetworkError(f"{type(exc).__name__}: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def httpx_default(httpx_mod):
    return httpx_mod.USE_CLIENT_DEFAULT


class _HttpxStream:
    def __init__(self, response, httpx_mod):
        self._r = response
        self._httpx = httpx_mod
        self.status = response.status_code
        self.headers = dict(response.headers)

    async def iter_chunks(self, size: int):
        try:
            async for chunk in self._r.aiter_bytes(size):
                yield chunk
        except self._httpx.HTTPError as exc:
            raise NetworkError(f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Limitador simples (intervalo mínimo entre requisições)
# ---------------------------------------------------------------------------


class RateLimiter:
    def __init__(self, per_minute: int = 240, *, clock=time.monotonic, sleep=asyncio.sleep):
        self._interval = 60.0 / per_minute if per_minute and per_minute > 0 else 0.0
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock: Optional[asyncio.Lock] = None

    async def wait(self) -> None:
        if not self._interval:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = self._clock()
            if now < self._next:
                await self._sleep(self._next - now)
                now = self._clock()
            self._next = max(now, self._next) + self._interval


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------

_STATUS_EXC = {401: AuthenticationError, 403: ForbiddenError, 404: ResourceNotFoundError}


def error_message(resp: Response) -> str:
    body = resp.json()
    if isinstance(body, dict):
        for key in ("userMessage", "error_description", "description", "message", "error"):
            if body.get(key):
                return str(body[key])
    return f"HTTP {resp.status}"


def raise_for_status(resp: Response) -> None:
    """Converte status >= 400 na exceção certa do projeto."""
    if resp.status < 400:
        return
    msg = error_message(resp)
    if resp.status == 429:
        try:
            retry_after = float(resp.header("Retry-After", "0") or 0)
        except ValueError:
            retry_after = 0.0
        raise RateLimitError(msg, retry_after=retry_after)
    exc = _STATUS_EXC.get(resp.status)
    if exc:
        raise exc(msg)
    raise ApiError(resp.status, msg)


class HttpClient:
    """Backend + rate limit + retry. Não conhece a API do Tidal."""

    def __init__(
        self,
        backend: Backend,
        *,
        requests_per_minute: int = 240,
        retries: int = 3,
        base_delay: float = 1.0,
        sleep=asyncio.sleep,
        clock=time.monotonic,
    ):
        self.backend = backend
        self._limiter = RateLimiter(requests_per_minute, clock=clock, sleep=sleep)
        self._retries = max(1, retries)
        self._base_delay = base_delay
        self._sleep = sleep

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        data: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
        retry: bool = True,
    ) -> Response:
        """Faz a requisição com retry em rede/5xx/429. NÃO levanta por 4xx
        (quem chama decide, p.ex. para tentar refresh de token no 401)."""
        attempts = self._retries if retry else 1
        resp: Optional[Response] = None
        for attempt in range(1, attempts + 1):
            await self._limiter.wait()
            try:
                resp = await self.backend.request(
                    method, url, params=params, data=data, headers=headers, timeout=timeout
                )
            except NetworkError as exc:
                if attempt >= attempts:
                    raise
                logger.debug("rede falhou (%s), tentativa %d/%d", exc, attempt, attempts)
                await self._sleep(self._base_delay * 2 ** (attempt - 1))
                continue
            if (resp.status == 429 or resp.status >= 500) and attempt < attempts:
                try:
                    wait = float(resp.header("Retry-After", "0") or 0)
                except ValueError:
                    wait = 0.0
                wait = max(wait, self._base_delay * 2 ** (attempt - 1))
                logger.debug("HTTP %s em %s; nova tentativa em %.1fs", resp.status, url, wait)
                await self._sleep(min(wait, 60.0))
                continue
            return resp
        assert resp is not None
        return resp

    def stream(self, url: str, *, headers: Optional[dict] = None, timeout: Optional[float] = None):
        return self.backend.stream(url, headers=headers, timeout=timeout)

    async def aclose(self) -> None:
        await self.backend.aclose()


def create_client(**kwargs: Any) -> HttpClient:
    """Cria o cliente real. Falha com mensagem clara se httpx faltar."""
    try:
        backend = HttpxBackend(timeout=kwargs.pop("timeout", 30.0), verify=kwargs.pop("verify", True))
    except ImportError as exc:  # pragma: no cover
        raise TidalDLException(
            "O pacote 'httpx' não está instalado. Rode: pip install httpx"
        ) from exc
    return HttpClient(backend, **kwargs)
