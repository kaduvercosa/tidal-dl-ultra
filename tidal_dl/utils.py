"""Utilitários puros do tidal-dl-ultra (sem rede, sem mutagen).

Tudo aqui roda em qualquer ambiente, inclusive a-Shell/iOS: só biblioteca
padrão. Espelha o papel do utils.py do qobuz-dl-ultra.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import string
import unicodedata
from typing import Any, Awaitable, Callable, Iterable, Optional, TypeVar
from urllib.parse import urlsplit

from tidal_dl.constants import IMAGE_URL, OK_MAX_CHARACTER_LENGTH

logger = logging.getLogger(__name__)
T = TypeVar("T")

# ---------------------------------------------------------------------------
# Plataforma / caminhos
# ---------------------------------------------------------------------------


def is_ios() -> bool:
    """True em iOS/iPadOS (a-Shell): $HOME dentro de Containers/Data/Application."""
    if os.environ.get("TIDAL_DL_IOS_HOME"):
        return True
    return "Containers/Data/Application" in os.environ.get("HOME", "")


def _user_config_dir() -> str:
    try:  # platformdirs é opcional aqui (no a-Shell pode não estar instalado)
        import platformdirs

        return platformdirs.user_config_dir()
    except Exception:
        if os.name == "nt":
            return os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(os.path.expanduser("~"), ".config")


def get_config_paths() -> dict:
    """Resolve os caminhos de config (Windows, Linux/macOS e iOS/a-Shell).

    Ordem: ``CONFIG_DIR`` > ``TIDAL_DL_IOS_HOME`` > auto-detecção do a-Shell
    (``~/Documents``) > padrão do sistema. Mesma lógica do qobuz-dl-ultra.
    """
    ios_home = os.environ.get("TIDAL_DL_IOS_HOME")
    config_dir = os.environ.get("CONFIG_DIR")
    if not config_dir:
        if ios_home:
            config_dir = ios_home
        elif "Containers/Data/Application" in os.environ.get("HOME", ""):
            config_dir = os.path.join(os.environ["HOME"], "Documents")
        else:
            config_dir = _user_config_dir()
    config_path = os.path.join(config_dir, "tidal-dl")
    return {
        "config_dir": config_dir,
        "config_path": config_path,
        "config_file": os.path.join(config_path, "config.ini"),
        "tidal_db": os.path.join(config_path, "tidal_dl.db"),
        "library_db": os.path.join(config_path, "library.db"),
        "credentials_file": os.path.join(config_path, "credentials.json"),
    }


def default_download_folder() -> str:
    ios_home = os.environ.get("TIDAL_DL_IOS_HOME")
    if ios_home:
        return os.path.join(ios_home, "TidalDownloads")
    if "Containers/Data/Application" in os.environ.get("HOME", ""):
        return os.path.join(os.environ["HOME"], "Documents", "TidalDownloads")
    return "TidalDownloads"


def default_video_folder() -> str:
    """Mesma auto-detecção de ``default_download_folder()``, para vídeos.

    ANTES: ``video_directory`` no settings.py tinha só o literal relativo
    "TidalVideos" como default (sem passar por is_ios()/Containers), então
    no a-Shell ela resolvia relativa ao CWD (às vezes fora de ~/Documents,
    dependendo de onde o a-Shell foi aberto) em vez de sempre cair dentro
    de ~/Documents como a pasta de música principal já fazia.
    """
    ios_home = os.environ.get("TIDAL_DL_IOS_HOME")
    if ios_home:
        return os.path.join(ios_home, "TidalVideos")
    if "Containers/Data/Application" in os.environ.get("HOME", ""):
        return os.path.join(os.environ["HOME"], "Documents", "TidalVideos")
    return "TidalVideos"


# ---------------------------------------------------------------------------
# Binários externos (opcionais)
# ---------------------------------------------------------------------------

_BINARIOS: dict[str, Optional[str]] = {}


def encontrar_binario(nome: str) -> Optional[str]:
    """Procura um executável no PATH e em ``$APPDIR/bin`` (a-Shell). Com cache."""
    if nome in _BINARIOS:
        return _BINARIOS[nome]
    caminho = shutil.which(nome)
    if not caminho:
        appdir = os.environ.get("APPDIR", "")
        if appdir and os.path.isdir(os.path.join(appdir, "bin")):
            caminho = shutil.which(nome, path=os.path.join(appdir, "bin"))
    _BINARIOS[nome] = caminho
    return caminho


# ---------------------------------------------------------------------------
# Nomes de arquivo
# ---------------------------------------------------------------------------

_RESERVED = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WIN_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10)),
}


def sanitize_component(value: Any, *, max_len: int = 200) -> str:
    """Torna um valor seguro como UM componente de caminho.

    ``/`` vira `` - `` (AC/DC não cria subpasta), caracteres reservados somem,
    espaços/pontos finais saem (Windows) e nomes reservados ganham ``_``.
    """
    s = unicodedata.normalize("NFC", str(value if value is not None else ""))
    s = s.replace("/", " - ")
    s = _RESERVED.sub("", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if s.lower() in _WIN_RESERVED_NAMES:
        s = f"_{s}"
    return s[:max_len].rstrip(". ") or "_"


def truncate_name(name: str, budget: int = OK_MAX_CHARACTER_LENGTH, ext: str = "") -> str:
    """Corta ``name`` para caber em ``budget`` caracteres contando a extensão."""
    room = max(8, budget - len(ext))
    return name if len(name) <= room else name[:room].rstrip(". ")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


def validate_template(template: str, allowed: Iterable[str]) -> list[str]:
    """Devolve os placeholders desconhecidos de ``template`` (lista vazia = ok)."""
    allowed = set(allowed)
    unknown = []
    try:
        for _, field, _, _ in string.Formatter().parse(template):
            if field and field.split(".")[0].split("[")[0] not in allowed:
                unknown.append(field)
    except ValueError as exc:
        return [f"template inválido: {exc}"]
    return unknown


def render_template(template: str, values: dict, fallback: str) -> str:
    """Formata ``template``; se faltar chave ou for inválido, usa ``fallback``."""
    try:
        return template.format_map(_SafeDict(values))
    except (KeyError, IndexError, ValueError):
        return fallback.format_map(_SafeDict(values)) if "{" in fallback else fallback


def render_path(template: str, values: dict, fallback: str) -> str:
    """Como render_template, mas os VALORES e cada componente separado por ``/`` são sanitizados."""
    values = {
        k: (sanitize_component(v) if isinstance(v, str) and v.strip() else v)
        for k, v in values.items()
    }
    raw = render_template(template, values, fallback)
    parts = [sanitize_component(p) for p in raw.replace("\\", "/").split("/") if p.strip()]
    return os.path.join(*parts) if parts else "_"


# ---------------------------------------------------------------------------
# URLs do Tidal
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"(?:^|/)(album|track|playlist|artist|mix|video|v)/([0-9A-Za-z-]+)", re.IGNORECASE
)


def parse_url(url: str) -> Optional[tuple[str, str]]:
    """``https://tidal.com/browse/album/123`` -> ``("album", "123")``.

    Aceita tidal.com, listen.tidal.com, ``tidal://album/123`` e links com
    ``/track/456`` no fim (nesse caso o alvo é a FAIXA). None se não reconhecer.
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("tidal://"):
        path = url[len("tidal://"):]
    else:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        host = (parts.netloc or "").lower()
        if host and "tidal.com" not in host:
            return None
        path = parts.path
    found = _URL_RE.findall("/" + path.strip("/"))
    if not found:
        return None
    kind, ident = found[-1]  # o último segmento manda (/album/1/track/2 -> track 2)
    kind = kind.lower()
    if kind == "v":
        kind = "video"
    if kind == "mix":
        return None
    if kind != "playlist" and not ident.isdigit():
        return None
    return kind, ident


def cover_url(cover_uuid: Optional[str], size: int = 1280) -> Optional[str]:
    """URL da capa a partir do UUID (``a-b-c`` -> ``a/b/c``)."""
    if not cover_uuid:
        return None
    return f"{IMAGE_URL}/{cover_uuid.replace('-', '/')}/{size}x{size}.jpg"


# ---------------------------------------------------------------------------
# Formatação humana
# ---------------------------------------------------------------------------


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def human_duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "--:--"
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Retry assíncrono (sem tenacity: menos dependências no a-Shell)
# ---------------------------------------------------------------------------


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    give_up_on: tuple[type[BaseException], ...] = (asyncio.CancelledError, KeyboardInterrupt),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_retry: Optional[Callable[[int, BaseException], None]] = None,
) -> T:
    """Executa ``fn`` com backoff exponencial. Relevanta a última exceção."""
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except give_up_on:
            raise
        except retry_on as exc:
            last = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            retry_after = getattr(exc, "retry_after", 0) or 0
            delay = max(delay, float(retry_after))
            if on_retry:
                on_retry(attempt, exc)
            await sleep(delay)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Arquivos
# ---------------------------------------------------------------------------


def atomic_write_text(path: str, text: str, *, mode: Optional[int] = None) -> None:
    """Grava texto de forma atômica (tmp + replace); ``mode`` ex.: 0o600."""
    tmp = f"{path}.tmp"
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode if mode is not None else 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def remove_quiet(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def clean_leftovers(directory: str, tmp_prefix: str) -> int:
    """Apaga arquivos temporários órfãos (``~tmp_*``) sob ``directory``."""
    n = 0
    for cur, _dirs, files in os.walk(directory):
        for f in files:
            if f.startswith(tmp_prefix):
                remove_quiet(os.path.join(cur, f))
                n += 1
    return n
