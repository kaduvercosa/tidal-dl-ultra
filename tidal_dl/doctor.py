"""``tidal-dl doctor``: checagem de saúde do ambiente, do config e dos bancos.

ORIGEM DA IDEIA
---------------
Portado dos scripts de diagnóstico do libsync (``env-doctor.sh``,
``check-sentinels.sh``, ``album-status-report.sh``, ``check-dedup.sh``),
unificados num comando só. Regras herdadas:

  * SOMENTE LEITURA: não instala nada, não faz rede, não grava nada.
  * Níveis PASS / WARN / FAIL; só FAIL derruba o código de saída.
  * Nunca imprime segredo (tokens/senhas): só diz se existem.

As checagens recebem caminhos por parâmetro (nada de globais), então rodam
igual em teste e em produção -- e funcionam mesmo com o config.ini quebrado,
que é justamente quando alguém roda o ``doctor``.
"""

from __future__ import annotations

import configparser
import importlib
import os
import sqlite3
import stat
import sys
from dataclasses import asdict, dataclass
from typing import Optional

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

REQUIRED_MODULES = ["httpx", "mutagen", "colorama"]
OPTIONAL_MODULES = {
    "keyring": "guardar o token no cofre do sistema (não existe no a-Shell)",
    "platformdirs": "pasta de config padrão do sistema",
    "rapidfuzz": "match fuzzy mais rápido no scan",
    "brotli": "respostas HTTP comprimidas",
}


@dataclass
class Check:
    level: str
    name: str
    detail: str = ""


def check_python() -> Check:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v < (3, 10):
        return Check(FAIL, "Python", f"{ver}: o projeto exige >= 3.10")
    return Check(PASS, "Python", ver)


def check_modules() -> list[Check]:
    out: list[Check] = []
    missing = []
    for mod in REQUIRED_MODULES:
        try:
            importlib.import_module(mod)
        except Exception:
            missing.append(mod)
    if missing:
        out.append(Check(FAIL, "Dependências", "faltando: " + ", ".join(missing)
                         + " (pip install -U tidal-dl-ultra)"))
    else:
        out.append(Check(PASS, "Dependências", "todas as obrigatórias importam"))
    for mod, why in OPTIONAL_MODULES.items():
        try:
            importlib.import_module(mod)
            out.append(Check(PASS, f"Opcional: {mod}", why))
        except Exception:
            out.append(Check(WARN, f"Opcional: {mod}", f"ausente ({why})"))
    return out


def check_binaries() -> list[Check]:
    from tidal_dl.utils import encontrar_binario

    path = encontrar_binario("ffmpeg")
    if path:
        return [Check(PASS, "ffmpeg", path + " (opcional: só como plano B do remux)")]
    return [Check(PASS, "ffmpeg", "ausente (ok: o remux FLAC é feito em Python puro)")]


def check_platform() -> list[Check]:
    from tidal_dl.utils import is_ios

    if is_ios():
        return [Check(PASS, "Plataforma", "iOS/a-Shell detectado (sem keyring; token em arquivo 0600)")]
    return [Check(PASS, "Plataforma", sys.platform)]


def check_config(config_file: str) -> list[Check]:
    out: list[Check] = []
    if not os.path.isfile(config_file):
        return [Check(WARN, "config.ini", f"não encontrado: {config_file} (usará os padrões; `tidal-dl config` cria)")]
    out.append(Check(PASS, "config.ini", config_file))
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(config_file).st_mode)
        out.append(Check(PASS if not mode & 0o077 else WARN, "Permissão do config", oct(mode)))
    try:
        from tidal_dl.settings import TidalDLSettings

        TidalDLSettings.from_config(config_file).validate()
        out.append(Check(PASS, "Valores do config", "válidos"))
    except Exception as exc:
        out.append(Check(FAIL, "Valores do config", str(exc)))
    return out


def check_credentials(credentials_file: str, *, use_keyring: bool = False) -> list[Check]:
    """Confere o login SEM mostrar o token: só existência, permissão e validade."""
    import json
    import time

    creds = None
    where = credentials_file
    if use_keyring:
        try:
            import keyring

            blob = keyring.get_password("tidal-dl-ultra", "credentials")
            if blob:
                creds, where = json.loads(blob), "keyring"
        except Exception:
            pass
    if creds is None:
        if not os.path.isfile(credentials_file):
            return [Check(WARN, "Login", "não logado (rode `tidal-dl login`)")]
        try:
            with open(credentials_file, encoding="utf-8") as fh:
                creds = json.load(fh)
        except (OSError, ValueError) as exc:
            return [Check(FAIL, "Login", f"credentials.json ilegível: {exc}")]
    out = []
    if where != "keyring" and os.name == "posix":
        mode = stat.S_IMODE(os.stat(credentials_file).st_mode)
        if mode & 0o077:
            out.append(Check(WARN, "Permissão do token", f"{oct(mode)}: outros usuários podem ler (chmod 600)"))
    if not creds.get("access_token"):
        return out + [Check(FAIL, "Login", "sem access_token (rode `tidal-dl login`)")]
    left = float(creds.get("token_expiry") or 0) - time.time()
    method = creds.get("auth_method", "?")
    note = "" if method == "pkce" else " (device-code: limitado a AAC; use `tidal-dl login` para Lossless)"
    if left <= 0 and not creds.get("refresh_token"):
        out.append(Check(FAIL, "Login", "token expirado e sem refresh_token"))
    elif left <= 0:
        out.append(Check(PASS, "Login", f"token expirado, mas há refresh_token (renova sozinho); método={method}{note}"))
    else:
        out.append(Check(PASS, "Login", f"token válido por {int(left // 3600)}h; método={method}{note}"))
    return out


def check_directory(directory: Optional[str]) -> list[Check]:
    if not directory:
        return [Check(WARN, "Pasta de downloads", "não definida")]
    directory = os.path.expanduser(directory)
    if not os.path.isdir(directory):
        return [Check(WARN, "Pasta de downloads", f"ainda não existe: {directory}")]
    out = [Check(PASS, "Pasta de downloads", directory)]
    if not os.access(directory, os.W_OK):
        out.append(Check(FAIL, "Escrita na pasta", "sem permissão de escrita"))
    leftovers = stuck = 0
    for _, dirs, files in os.walk(directory):
        leftovers += sum(1 for f in files if f.startswith("~tmp_"))
        stuck += sum(1 for d in dirs if d.startswith("[IN PROGRESS]"))
    if leftovers:
        out.append(Check(WARN, "Temporários", f"{leftovers} arquivo(s) ~tmp_ de downloads interrompidos"))
    if stuck:
        out.append(Check(WARN, "Pastas [IN PROGRESS]", f"{stuck} (execução interrompida; rode o download de novo)"))
    return out


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def check_downloads_db(db_path: str) -> list[Check]:
    if not os.path.isfile(db_path):
        return [Check(WARN, "tidal_dl.db", "ainda não existe (criado no primeiro download)")]
    try:
        conn = _ro(db_path)
        try:
            integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integ != "ok":
                return [Check(FAIL, "tidal_dl.db", f"integrity_check: {integ}")]
            n = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
            paths = conn.execute(
                "SELECT saved_path FROM downloads WHERE media_type='album' AND saved_path != ''"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [Check(FAIL, "tidal_dl.db", str(exc))]
    out = [Check(PASS, "tidal_dl.db", f"{n} registro(s), integridade ok")]
    stale = sum(1 for (p,) in paths if not os.path.exists(p))
    if stale:
        out.append(Check(WARN, "Registros obsoletos", f"{stale} álbum(ns) apontam para pasta inexistente "
                         "(o downloader os descarta sozinho ao tentar baixar)"))
    return out


def check_library_db(lib_path: str) -> list[Check]:
    if not os.path.isfile(lib_path):
        return [Check(WARN, "library.db", "ainda não existe (rode `tidal-dl sync-favorites`)")]
    try:
        conn = _ro(lib_path)
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
            stuck = conn.execute(
                "SELECT COUNT(*) FROM albums WHERE download_status IN ('queued','downloading')"
            ).fetchone()[0]
            nofolder = conn.execute(
                "SELECT COUNT(*) FROM albums WHERE download_status='complete' "
                "AND (local_folder_path IS NULL OR local_folder_path='')"
            ).fetchone()[0]
            gone = [r["local_folder_path"] for r in conn.execute(
                "SELECT local_folder_path FROM albums WHERE download_status='complete' "
                "AND local_folder_path IS NOT NULL AND local_folder_path != ''"
            )]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [Check(FAIL, "library.db", str(exc))]
    out = [Check(PASS, "library.db", f"{total} álbum(ns)")]
    if stuck:
        out.append(Check(WARN, "Álbuns presos", f"{stuck} em queued/downloading (`tidal-dl library reset-stuck`)"))
    if nofolder:
        out.append(Check(WARN, "Sem pasta registrada", f"{nofolder} 'complete' sem local_folder_path (`tidal-dl scan`)"))
    missing = sum(1 for p in gone if not os.path.isdir(p))
    if missing:
        out.append(Check(WARN, "Pasta sumiu", f"{missing} álbum(ns) 'complete' sem pasta (`tidal-dl library reconcile --fix`)"))
    return out


def check_sentinels(directory: Optional[str]) -> list[Check]:
    if not directory or not os.path.isdir(os.path.expanduser(directory)):
        return []
    from tidal_dl.sentinel import discover_sentinels, sentinel_identity, validate_folder

    records, failures, _ = discover_sentinels(os.path.expanduser(directory))
    bad = len(failures)
    for r in records:
        try:
            sentinel_identity(r.payload)
            if validate_folder(r.folder, r.payload):
                bad += 1
        except ValueError:
            bad += 1
    if bad:
        return [Check(WARN, "Sentinelas", f"{len(records)} válida(s) lidas, {bad} com problema (`tidal-dl library reconcile`)")]
    return [Check(PASS, "Sentinelas", f"{len(records)} álbum(ns) com sentinela consistente")]


def run_checks(
    *, config_file: str, downloads_db: str, library_db: str, directory: Optional[str],
    credentials_file: Optional[str] = None,
) -> list[Check]:
    results: list[Check] = [check_python()]
    results += check_platform()
    results += check_modules()
    results += check_binaries()
    results += check_config(config_file)
    if credentials_file:
        results += check_credentials(credentials_file)
    results += check_directory(directory)
    results += check_downloads_db(downloads_db)
    results += check_library_db(library_db)
    results += check_sentinels(directory)
    return results


def resolve_directory(config_file: str) -> Optional[str]:
    """Lê ``directory`` sem exigir config válido."""
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(config_file, encoding="utf-8")
    except configparser.Error:
        return None
    return cfg.get("tidal", "directory", fallback=None)


def render(results: list[Check]) -> int:
    """Imprime pela camada ``ui`` e devolve o código de saída (1 se houve FAIL)."""
    from tidal_dl import ui

    ui.banner("TIDAL-DL-ULTRA  ·  DOCTOR")
    for r in results:
        line = f"{r.level:<4}  {r.name}" + (f": {r.detail}" if r.detail else "")
        if r.level == FAIL:
            ui.error(line)
        elif r.level == WARN:
            ui.warn(line)
        else:
            ui.emit_always(f"  {line}")
    fails = sum(1 for r in results if r.level == FAIL)
    warns = sum(1 for r in results if r.level == WARN)
    ui.blank()
    if fails:
        ui.error(f"{fails} verificação(ões) FALHARAM, {warns} aviso(s).")
    else:
        ui.ok(f"Nenhuma falha. {warns} aviso(s) informativo(s).")
    return 1 if fails else 0


def to_json(results: list[Check]) -> list[dict]:
    return [asdict(r) for r in results]
