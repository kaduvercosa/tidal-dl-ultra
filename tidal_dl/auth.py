"""Autenticação OAuth do Tidal: PKCE (Lossless/Hi-Res) e device-code (AAC).

FLUXOS
------
* **PKCE** (padrão): abre uma URL no navegador, você entra na conta e cola de
  volta a URL para a qual o Tidal redirecionou. Funciona em terminal sem
  navegador (a-Shell/SSH): a página final dá 404, o que vale é o ``code=`` na
  barra de endereço. Libera LOSSLESS / HI_RES / HI_RES_LOSSLESS conforme o plano.
* **Device-code** (``--device``): mostra um código para digitar em
  link.tidal.com. Mais simples, mas o cliente é limitado a AAC ~320 kbps.

As credenciais de cliente abaixo são públicas na comunidade de ferramentas
Tidal (vêm dos apps oficiais). Podem ser trocadas por variáveis de ambiente
(``TIDAL_DL_CLIENT_ID``, ``TIDAL_DL_CLIENT_SECRET``, ``TIDAL_DL_CLIENT_ID_PKCE``,
``TIDAL_DL_CLIENT_SECRET_PKCE``) se o Tidal as revogar.

ARMAZENAMENTO
-------------
Tokens ficam em ``credentials.json`` (permissão 0600) ao lado do config.ini, ou
no keyring do sistema quando ele existe e ``use_keyring`` está ligado. No
a-Shell não há keyring: o arquivo dentro do sandbox do app é o caminho normal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from tidal_dl.constants import AUTH_URL
from tidal_dl.exceptions import AuthenticationError
from tidal_dl.net import HttpClient, error_message
from tidal_dl.utils import atomic_write_text

logger = logging.getLogger(__name__)

PKCE_AUTHORIZE_URL = "https://login.tidal.com/authorize"
PKCE_REDIRECT_URI = "https://tidal.com/android/login/auth"
SCOPE = "r_usr+w_usr+w_sub"
KEYRING_SERVICE = "tidal-dl-ultra"


def _b64(s: str) -> str:
    return base64.b64decode(s).decode("ascii")


CLIENT_ID = os.environ.get("TIDAL_DL_CLIENT_ID") or _b64("ZlgySnhkbW50WldLMGl4VA==")
CLIENT_SECRET = os.environ.get("TIDAL_DL_CLIENT_SECRET") or _b64(
    "MU5tNUFmREFqeHJnSkZKYktOV0xlQXlLR1ZHbUlOdVhQUExIVlhBdnhBZz0="
)
CLIENT_ID_PKCE = os.environ.get("TIDAL_DL_CLIENT_ID_PKCE") or _b64("NkJEU1JkcEs5aHFFQlRnVQ==")
CLIENT_SECRET_PKCE = os.environ.get("TIDAL_DL_CLIENT_SECRET_PKCE") or _b64(
    "eGV1UG1ZN25icFo5SUliTEFjUTkzc2hrYTFWTmhlVUFxTjZJY3N6alRHOD0="
)

METHOD_PKCE = "pkce"
METHOD_DEVICE = "device_code"


# ---------------------------------------------------------------------------
# Credenciais
# ---------------------------------------------------------------------------


@dataclass
class Credentials:
    access_token: str
    refresh_token: str = ""
    token_expiry: float = 0.0
    user_id: str = ""
    country_code: str = "US"
    auth_method: str = METHOD_PKCE
    extra: dict = field(default_factory=dict)

    def expires_in(self, now: Optional[float] = None) -> float:
        return self.token_expiry - (time.time() if now is None else now)

    def is_stale(self, window: float = 300.0, now: Optional[float] = None) -> bool:
        """True se expira dentro de ``window`` segundos (token_expiry 0 = desconhecido)."""
        return self.token_expiry > 0 and self.expires_in(now) < window

    @classmethod
    def from_dict(cls, d: dict) -> "Credentials":
        return cls(
            access_token=str(d.get("access_token") or ""),
            refresh_token=str(d.get("refresh_token") or ""),
            token_expiry=float(d.get("token_expiry") or 0),
            user_id=str(d.get("user_id") or ""),
            country_code=str(d.get("country_code") or "US"),
            auth_method=str(d.get("auth_method") or METHOD_DEVICE),
            extra=dict(d.get("extra") or {}),
        )

    def to_dict(self) -> dict:
        return asdict(self)


class CredentialStore:
    """Guarda/lê ``Credentials`` (keyring opcional + arquivo 0600)."""

    def __init__(self, path: str, *, use_keyring: bool = False):
        self.path = path
        self.use_keyring = use_keyring

    def _keyring(self):
        if not self.use_keyring:
            return None
        try:
            import keyring

            name = keyring.get_keyring().__class__.__name__.lower()
            if "fail" in name or "null" in name:
                return None
            return keyring
        except Exception:
            return None

    def save(self, creds: Credentials) -> str:
        """Devolve onde gravou: ``"keyring"`` ou o caminho do arquivo."""
        blob = json.dumps(creds.to_dict(), ensure_ascii=False)
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(KEYRING_SERVICE, "credentials", blob)
                if os.path.exists(self.path):
                    os.remove(self.path)  # não deixa segredo duplicado em disco
                return "keyring"
            except Exception as exc:
                logger.debug("keyring indisponível (%s); usando arquivo", exc)
        atomic_write_text(self.path, blob, mode=0o600)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        return self.path

    def load(self) -> Optional[Credentials]:
        kr = self._keyring()
        if kr is not None:
            try:
                blob = kr.get_password(KEYRING_SERVICE, "credentials")
                if blob:
                    return Credentials.from_dict(json.loads(blob))
            except Exception:
                pass
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        creds = Credentials.from_dict(data) if isinstance(data, dict) else None
        return creds if creds and creds.access_token else None

    def delete(self) -> bool:
        removed = False
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(KEYRING_SERVICE, "credentials")
                removed = True
            except Exception:
                pass
        try:
            os.remove(self.path)
            removed = True
        except OSError:
            pass
        return removed


# ---------------------------------------------------------------------------
# Parsing de tokens
# ---------------------------------------------------------------------------


def _creds_from_token_body(body: dict, method: str, *, previous: Optional[Credentials] = None) -> Credentials:
    if "access_token" not in body:
        raise AuthenticationError("Resposta de token sem access_token")
    user = body.get("user") or {}
    return Credentials(
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token") or (previous.refresh_token if previous else ""),
        token_expiry=time.time() + float(body.get("expires_in") or 0),
        user_id=str(user.get("userId") or (previous.user_id if previous else "")),
        country_code=str(user.get("countryCode") or (previous.country_code if previous else "US")),
        auth_method=method,
    )


def _basic(client_id: str, secret: str) -> dict:
    token = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


async def _post_form(http: HttpClient, path: str, data: dict, headers: Optional[dict] = None):
    return await http.request("POST", f"{AUTH_URL}/{path}", data=data, headers=headers)


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str, str]:
    """``(verifier, challenge_S256, client_unique_key)``."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge, secrets.token_hex(8)


def build_pkce_authorize_url(challenge: str, unique_key: str) -> str:
    params = {
        "response_type": "code",
        "redirect_uri": PKCE_REDIRECT_URI,
        "client_id": CLIENT_ID_PKCE,
        "lang": "EN",
        "appMode": "android",
        "client_unique_key": unique_key,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "restrict_signup": "true",
    }
    # safe=":/": o redirect_uri fica LEGÍVEL (sem %3A/%2F). Assim a URL não tem
    # nenhum "%" e não pode ser recodificada por terminal/navegador (o a-Shell e
    # o Safari transformavam %3A em %253A, o Tidal recusava o redirect_uri e o
    # login nunca terminava).
    return f"{PKCE_AUTHORIZE_URL}?{urlencode(params, safe=':/')}"


def extract_code_from_redirect(redirect_url: str) -> str:
    """Tira o ``code`` da URL colada (aceita também só o código puro)."""
    value = (redirect_url or "").strip()
    if not value:
        raise ValueError("Nada foi colado.")
    parts = urlsplit(value)
    if "code_challenge=" in value or (parts.netloc.endswith("login.tidal.com") and parts.path.startswith("/authorize")):
        hint = " (o '%25' mostra que a URL foi recodificada: abra a URL de novo, sem editar)" if "%25" in value else ""
        raise ValueError(
            "Você colou a URL de LOGIN (a que o programa mostrou), não a de RETORNO"
            f"{hint}. Abra-a no navegador, ENTRE na conta, autorize e só então copie a URL "
            "final da barra de endereço (começa com https://tidal.com/android/login/auth?code=...)."
        )
    if "code=" not in value:
        if "/" not in value and "?" not in value and len(value) >= 16:
            return value  # o usuário colou só o código
        raise ValueError(
            "A URL colada não tem 'code='. Copie a URL COMPLETA da barra de "
            "endereço depois que o Tidal redirecionar (a página pode dar 404)."
        )
    codes = parse_qs(urlsplit(value).query).get("code")
    if not codes:
        raise ValueError("A URL colada não tem o parâmetro 'code'.")
    return codes[0]


async def exchange_pkce_code(http: HttpClient, code: str, verifier: str, unique_key: str) -> Credentials:
    resp = await _post_form(
        http,
        "token",
        {
            "code": code,
            "client_id": CLIENT_ID_PKCE,
            "grant_type": "authorization_code",
            "redirect_uri": PKCE_REDIRECT_URI,
            "scope": SCOPE,
            "code_verifier": verifier,
            "client_unique_key": unique_key,
        },
    )
    if resp.status >= 400:
        raise AuthenticationError(f"Troca do código PKCE falhou: {error_message(resp)}")
    return _creds_from_token_body(resp.json(), METHOD_PKCE)


async def login_pkce(
    http: HttpClient,
    *,
    show_url: Callable[[str], None],
    ask_redirect: Callable[[], Awaitable[str]],
    on_error: Optional[Callable[[str], None]] = None,
    attempts: int = 3,
) -> Credentials:
    """Fluxo PKCE guiado: ``show_url(url)`` e ``ask_redirect()`` vêm da CLI.

    Se a URL colada estiver errada, avisa (``on_error``) e pergunta de novo até
    ``attempts`` vezes SEM gerar outro par PKCE: o link já aberto no navegador
    continua válido, então o usuário não precisa refazer o login do zero.
    """
    verifier, challenge, unique_key = generate_pkce_pair()
    show_url(build_pkce_authorize_url(challenge, unique_key))
    last: Optional[ValueError] = None
    for attempt in range(max(1, attempts)):
        try:
            code = extract_code_from_redirect(await ask_redirect())
        except ValueError as exc:
            last = exc
            if on_error and attempt < attempts - 1:
                on_error(str(exc))
            continue
        return await exchange_pkce_code(http, code, verifier, unique_key)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Device code
# ---------------------------------------------------------------------------


async def request_device_code(http: HttpClient) -> dict:
    resp = await _post_form(http, "device_authorization", {"client_id": CLIENT_ID, "scope": SCOPE})
    if resp.status >= 400:
        raise AuthenticationError(f"device_authorization falhou: {error_message(resp)}")
    return resp.json()


async def poll_device_code(http: HttpClient, device_code: str) -> tuple[str, Optional[Credentials]]:
    """``("ok", creds)`` | ``("pending", None)`` | ``("error", None)``."""
    resp = await _post_form(
        http,
        "token",
        {
            "client_id": CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "scope": SCOPE,
        },
        headers=_basic(CLIENT_ID, CLIENT_SECRET),
    )
    body = resp.json()
    if resp.status == 200 and isinstance(body, dict) and "access_token" in body:
        return "ok", _creds_from_token_body(body, METHOD_DEVICE)
    if isinstance(body, dict) and (
        body.get("sub_status") == 1002 or body.get("error") == "authorization_pending"
    ):
        return "pending", None
    return "error", None


async def login_device(
    http: HttpClient,
    *,
    show_code: Callable[[str, str], None],
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float] = time.monotonic,
) -> Credentials:
    """Device-code: chama ``show_code(url, user_code)`` e faz polling até autorizar."""
    info = await request_device_code(http)
    url = str(info.get("verificationUriComplete") or info.get("verificationUri") or "")
    if url and not url.startswith("http"):
        url = f"https://{url}"
    show_code(url, str(info.get("userCode") or ""))
    interval = max(1.0, float(info.get("interval") or 2))
    deadline = clock() + float(info.get("expiresIn") or 300)
    while clock() < deadline:
        state, creds = await poll_device_code(http, str(info["deviceCode"]))
        if state == "ok" and creds:
            return creds
        if state == "error":
            raise AuthenticationError("Autorização recusada ou inválida.")
        await sleep(interval)
    raise AuthenticationError("O código expirou antes de ser autorizado.")


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


async def refresh_credentials(http: HttpClient, creds: Credentials) -> Credentials:
    """Renova o access_token com o par de cliente que o emitiu."""
    if not creds.refresh_token:
        raise AuthenticationError("Sem refresh_token: faça login de novo (tidal-dl login).")
    if creds.auth_method == METHOD_PKCE:
        data = {
            "client_id": CLIENT_ID_PKCE,
            "client_secret": CLIENT_SECRET_PKCE,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
            "scope": SCOPE,
        }
        headers = None
    else:
        data = {
            "client_id": CLIENT_ID,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
            "scope": SCOPE,
        }
        headers = _basic(CLIENT_ID, CLIENT_SECRET)
    resp = await _post_form(http, "token", data, headers)
    if resp.status >= 400:
        raise AuthenticationError(f"Renovação do token falhou: {error_message(resp)}")
    return _creds_from_token_body(resp.json(), creds.auth_method, previous=creds)
