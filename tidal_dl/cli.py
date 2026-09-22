"""Ponto de entrada do tidal-dl-ultra (``tidal-dl``).

Comandos que NÃO precisam de login (doctor, scan, library, stats, config,
logout) rodam mesmo com token expirado ou config quebrado -- justamente
quando são necessários. Os demais criam ``TidalDL`` e renovam o token sozinhos.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Optional

from tidal_dl import db as dbm, ui
from tidal_dl.auth import CredentialStore, login_device, login_pkce
from tidal_dl.color import ACCENT_PRESETS, OFF, GREEN, YELLOW, BG, accent_preview
from tidal_dl.commands import build_parser
from tidal_dl.constants import QUALITY_LABELS, TMP_PREFIX
from tidal_dl.core import TidalDL, read_url_file
from tidal_dl.exceptions import (
    AuthenticationError,
    ConfigError,
    TidalDLException,
)
from tidal_dl.net import NetworkError, create_client
from tidal_dl.settings import TidalDLSettings
from tidal_dl.utils import clean_leftovers, get_config_paths

OFFLINE = {
    "doctor": "doctor", "check": "doctor", "scan": "scan", "library-scan": "scan",
    "library": "library", "lib": "library", "stats": "stats", "config": "config",
    "logout": "logout",
}
SYNC = {"sync-favorites", "sf"}
DOWNLOAD_CMDS = {"dl", "search", "fun", "i", "lucky"}


def _pick_accent_color() -> str:
    """Seletor interativo da cor de destaque para o assistente de configuração."""
    ui.emit(f"\n{BG}[?] Cor de destaque do programa:{OFF}")
    ui.wrapped("Aparece em nomes de faixas, cabeçalhos, barras e progresso.", indent=4)
    ui.blank()

    for idx, (name, _rgb, escape) in enumerate(ACCENT_PRESETS, 1):
        if escape:
            preview = accent_preview(escape, "━━ [FAIXA] ARTISTA The Weeknd")
            ui.emit(f" {idx:2}. {name:<22} {preview}")
        else:
            ui.emit(f" {idx:2}. {name}")

    ui.blank()
    while True:
        _n = len(ACCENT_PRESETS)
        prompt = f"Escolha (1-{_n}) [Enter = 1 padrão]: "
        if len(prompt) > ui.width():
            prompt = f"Escolha 1-{_n}: "
        choice = input(prompt).strip()
        if not choice:
            choice = "1"
        try:
            idx = int(choice)
            if 1 <= idx <= len(ACCENT_PRESETS):
                name, rgb, escape = ACCENT_PRESETS[idx - 1]
                break
        except ValueError:
            pass
        ui.emit(f" Por favor escolha entre 1 e {len(ACCENT_PRESETS)}.")

    if rgb is None:
        ui.emit("\n Digite os valores RGB separados por ponto e vírgula.")
        ui.emit(" Exemplo: 255;100;50 (vermelho), 0;200;150 (teal)\n")
        while True:
            raw = input(" Código RGB (R;G;B): ").strip()
            raw = raw.replace(",", ";").replace(" ", ";")
            parts_str = [x.strip() for x in raw.split(";") if x.strip()]
            try:
                parts = [int(x) for x in parts_str]
                if len(parts) == 3 and all(0 <= p <= 255 for p in parts):
                    rgb = ";".join(str(p) for p in parts)
                    escape = ui.c(f"\033[38;2;{parts[0]};{parts[1]};{parts[2]}m")
                    break
            except ValueError:
                pass
            ui.emit(" Formato inválido. Use três números de 0 a 255, ex: 150;80;220")

    ui.emit("\n Preview da sua cor:")
    ui.emit(accent_preview(escape, "━━ [FAIXA] ARTISTA The Weeknd"))
    confirm = input(" Confirmar esta cor? (Enter = sim, n = escolher outra): ").strip().lower()
    if confirm in ("n", "nao", "no"):
        return _pick_accent_color()

    ui.emit(f"\n {GREEN}Cor salva: {escape}━━ {name.strip()}{OFF}\n")
    return rgb


async def _reset_config(config_file: str) -> TidalDLSettings:
    """Executa o assistente de configuração interativa no terminal e salva no config.ini."""
    if ui.width() >= 41:
        ui.emit(f"\n{BG}[ TIDAL-DL-ULTRA - CONFIGURAÇÃO INICIAL ]{OFF}")
    else:
        ui.emit(f"\n{BG}[ CONFIGURAÇÃO INICIAL ]{OFF}")

    st = TidalDLSettings()
    accent_rgb = _pick_accent_color()
    st.accent_color = accent_rgb
    c_accent = ui.c(f"\033[38;2;{accent_rgb}m") if accent_rgb else ui.c(YELLOW)

    st.directory = os.path.expanduser(
        input(f"\nPasta de downloads de áudio [Padrão: '{st.directory}']:\n- ").strip() or st.directory
    )
    st.video_directory = os.path.expanduser(
        input(f"\nPasta de downloads de vídeos [Padrão: '{st.video_directory}']:\n- ").strip() or st.video_directory
    )

    ui.emit(f"\n{c_accent}[?] Qualidade de áudio padrão:{OFF}")
    for k, v in QUALITY_LABELS.items():
        ui.emit(f"  {k}: {v}")
    q_str = input(f"Escolha a qualidade (0-4) [Padrão: {st.quality}]:\n- ").strip()
    if q_str.isdigit() and int(q_str) in QUALITY_LABELS:
        st.quality = int(q_str)

    ui.emit(f"\n{c_accent}[?] Qualidade de vídeo padrão:{OFF}")
    ui.emit("  Opções: 1080p, 720p, 480p, 360p, max")
    vq = input(f"Qualidade de vídeo [Padrão: {st.video_quality}]:\n- ").strip().lower()
    if vq in ("1080p", "720p", "480p", "360p", "max"):
        st.video_quality = vq

    fetch_lyr = input("\nBaixar e embutir letras automaticamente? (yes/no) [Padrão: yes]\n- ").strip().lower()
    st.lyrics = False if fetch_lyr in ("no", "n", "false") else True

    if st.lyrics:
        ui.emit(f"\n{c_accent}[?] Idioma de Tradução de Letras:{OFF}")
        ui.emit("  Opções: pt (Português), en (Inglês), es (Espanhol), fr (Francês), original (Manter nativo)")
        lang = input("Idioma [Padrão: pt]:\n- ").strip().lower()
        st.lyrics_translation_lang = "" if lang in ("original", "orig") else (lang if lang in ("pt", "en", "es", "fr", "de", "it") else "pt")

        ui.emit(f"\n{c_accent}[!] Para usar o Genius como fallback de letras, insira seu API Token (Enter para pular):{OFF}")
        st.genius_token = input("Genius API Token:\n- ").strip()

    st.save(config_file)
    ui.ok(f"Configuração salva com sucesso em {config_file}!")
    return st


def _load_settings(paths: dict, args) -> TidalDLSettings:
    cfg = paths["config_file"]
    if not os.path.exists(cfg):
        try:
            st = TidalDLSettings.from_config(cfg)
        except ConfigError:
            st = TidalDLSettings()
    else:
        try:
            st = TidalDLSettings.from_config(cfg)
        except ConfigError as exc:
            ui.warn(f"Erro ao ler config.ini: {exc}")
            st = TidalDLSettings()
    return st.apply_args(args)


async def cmd_login(args, settings: TidalDLSettings, paths: dict, http=None) -> int:
    cfg_file = paths["config_file"]
    if not os.path.exists(cfg_file) and sys.stdin and sys.stdin.isatty():
        ui.info("Configuração inicial não encontrada. Iniciando assistente de configuração...")
        settings = await _reset_config(cfg_file)

    store = CredentialStore(paths["credentials_file"], use_keyring=not settings.disable_keyring)
    http = http or create_client()
    try:
        if getattr(args, "device", False):
            ui.warn("Login por dispositivo: limitado a AAC 320 kbps. Prefira `tidal-dl login`.")

            def show_code(url: str, code: str) -> None:
                ui.info("Abra o link abaixo, entre na conta e confirme:")
                ui.emit_always(f"\n  {url}\n")
                ui.info(f"Código: {code}")
                ui.step("Aguardando autorização...")

            creds = await login_device(http, show_code=show_code, sleep=asyncio.sleep)
        else:

            def show_url(url: str) -> None:
                ui.section("LOGIN")
                ui.info("1) Copie e abra a URL abaixo no navegador (Safari/Chrome).")
                ui.info("2) ENTRE na sua conta Tidal e autorize.")
                ui.info("3) A página final dá erro ou fica em branco: é normal. Copie a URL")
                ui.info("   da barra de endereço; ela COMEÇA com tidal.com/android/login/auth?code=")
                ui.info("   (não é a URL abaixo, que é só o começo do login).")
                ui.emit_always(f"\n{url}\n")
                ui.detail("Se o app do Tidal abrir sozinho no fim, abra o link de novo em outro "
                          "navegador ou em aba anônima.", indent=2)

            async def ask() -> str:
                return await asyncio.to_thread(input, "URL colada: ")

            creds = await login_pkce(http, show_url=show_url, ask_redirect=ask, on_error=ui.warn)
    except (AuthenticationError, ValueError) as exc:
        ui.error(f"Login falhou: {exc}")
        return 1
    except (EOFError, KeyboardInterrupt):
        ui.warn("Login cancelado.")
        return 130
    finally:
        await http.aclose()
    where = store.save(creds)
    ui.ok(f"Login feito (usuário {creds.user_id}, país {creds.country_code}, {creds.auth_method}).")
    ui.detail(f"Token salvo em: {where}")
    return 0


async def cmd_user(tidal: TidalDL) -> int:
    api = tidal.api
    ui.banner("TIDAL-DL-ULTRA  ·  CONTA")
    creds = api.creds
    ui.kv("Usuário (ID)", creds.user_id)
    ui.kv("País", creds.country_code)
    ui.kv("Login", creds.auth_method + ("" if creds.auth_method == "pkce" else " (só AAC)"))
    try:
        info = await api.user_info()
        if info.get("username") or info.get("email"):
            ui.kv("Conta", info.get("username") or info.get("email"))
        sub = await api.subscription()
        s = sub.get("subscription") or {}
        ui.kv("Plano", s.get("type", "?"))
        ui.kv("Status", sub.get("status", "?"))
        ui.kv("Qualidade máxima", sub.get("highestSoundQuality", "?"))
    except TidalDLException as exc:
        ui.warn(f"Não foi possível ler o plano: {exc}")
    return 0


def cmd_logout(paths: dict, settings: TidalDLSettings) -> int:
    store = CredentialStore(paths["credentials_file"], use_keyring=not settings.disable_keyring)
    if store.delete():
        ui.ok("Token apagado.")
    else:
        ui.info("Não havia token salvo.")
    return 0


# ---------------------------------------------------------------------------
# stats / config
# ---------------------------------------------------------------------------


def cmd_stats(paths: dict) -> int:
    st = dbm.get_stats(paths["tidal_db"])
    ui.banner("TIDAL-DL-ULTRA  ·  ESTATÍSTICAS")
    if not st["total"]:
        ui.info("Nenhum download registrado ainda.")
        return 0
    ui.kv("Registros", st["total"])
    for kind, n in st["by_type"].items():
        ui.kv(f"  {kind}", n)
    if st["by_format"]:
        ui.section("FAIXAS POR FORMATO")
        for fmt, n in st["by_format"].items():
            ui.kv(fmt, n)
    if st["by_year"]:
        ui.section("ÁLBUNS POR ANO")
        peak = max(st["by_year"].values())
        for year, n in st["by_year"].items():
            ui.kv(str(year), f"{ui.bar_gauge(n, peak, 12)} {n}")
    if st["latest"]:
        ui.section("ÚLTIMOS")
        for r in st["latest"]:
            ui.detail(f"{r['artist']} - {r['album'] or r['title']}", indent=2)
    return 0


async def _prompt(text: str, default: str) -> str:
    ans = await asyncio.to_thread(input, f"{text} [{default}]: ")
    return ans.strip() or default


async def cmd_config(args, paths: dict) -> int:
    cfg_file = paths["config_file"]
    if getattr(args, "reset", False):
        try:
            os.remove(cfg_file)
            ui.ok("config.ini apagado.")
        except FileNotFoundError:
            ui.info("Não havia config.ini.")
        return 0

    try:
        st = TidalDLSettings.from_config(cfg_file)
    except ConfigError as exc:
        ui.warn(f"{exc} -- começando dos padrões.")
        st = TidalDLSettings()

    if getattr(args, "show", False):
        ui.banner("TIDAL-DL-ULTRA  ·  CONFIG")
        ui.kv("Arquivo", cfg_file)
        for key, val in vars(st).items():
            ui.kv(key, val)
        return 0

    ui.banner("TIDAL-DL-ULTRA  ·  CONFIGURAÇÃO")
    ui.info("Enter mantém o valor entre colchetes.")
    st.directory = os.path.expanduser(await _prompt("Pasta de downloads de áudio", st.directory))
    st.video_directory = os.path.expanduser(await _prompt("Pasta de downloads de vídeos", st.video_directory))

    for k, v in QUALITY_LABELS.items():
        ui.detail(f"{k} = {v}", indent=2)
    while True:
        q = await _prompt("Qualidade de áudio (0-4)", str(st.quality))
        if q.isdigit() and int(q) in QUALITY_LABELS:
            st.quality = int(q)
            break
        ui.warn("Digite um número de 0 a 4.")

    st.video_quality = await _prompt("Qualidade de vídeo (1080p, 720p, 480p, 360p, max)", st.video_quality)
    st.folder_format = await _prompt("Formato da pasta", st.folder_format)
    st.track_format = await _prompt("Formato da faixa", st.track_format)
    st.concurrency = int(await _prompt("Faixas simultâneas (1-8)", str(st.concurrency)) or st.concurrency)
    lyr = await _prompt("Baixar letras? (s/n)", "s" if st.lyrics else "n")
    st.lyrics = lyr.lower().startswith(("s", "y"))
    if st.lyrics:
        st.genius_token = await _prompt("Genius API Token", st.genius_token)

    try:
        st.validate()
    except ConfigError as exc:
        ui.error(str(exc))
        return 1
    st.save(cfg_file)
    ui.ok(f"Configuração salva em {cfg_file}")
    return 0


# ---------------------------------------------------------------------------
# despacho
# ---------------------------------------------------------------------------


def _expand_sources(sources: list[str]) -> list[str]:
    urls: list[str] = []
    for s in sources:
        if s.lower().endswith(".txt") and os.path.isfile(os.path.expanduser(s)):
            urls.extend(read_url_file(s))
        else:
            urls.append(s)
    return urls


async def run_online(command: str, args, settings: TidalDLSettings, paths: dict) -> int:
    tidal = TidalDL(settings, paths=paths)
    try:
        await tidal.initialize()
    except AuthenticationError as exc:
        ui.error(str(exc))
        return 1
    try:
        if command in ("user", "me"):
            return await cmd_user(tidal)
        if command == "dl":
            summary = await tidal.download_urls(_expand_sources(args.SOURCE), include_eps=args.eps)
            ui.blank()
            ui.info(f"Concluído: {summary['ok']} ok, {summary['failed']} com falha.")
            return 1 if summary["failed"] else 0
        if command in ("search", "fun", "i"):
            ok = await tidal.interactive(" ".join(args.QUERY), args.type)
            return 0 if ok else 1
        if command == "lucky":
            return 0 if await tidal.lucky(" ".join(args.QUERY), args.type, args.number) else 1
        if command in SYNC:
            from tidal_dl.library_cmd import cmd_sync_favorites

            return await cmd_sync_favorites(args, tidal)
        raise TidalDLException(f"comando desconhecido: {command}")
    finally:
        await tidal.aclose()
        if os.path.isdir(settings.directory):
            clean_leftovers(settings.directory, TMP_PREFIX)


async def async_main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui.configure(quiet=args.quiet, verbose=args.verbose, color=not args.no_color)
    ui.install_logging()

    command = args.command
    paths = get_config_paths()
    if not command:
        from tidal_dl.welcome import print_welcome

        try:
            settings = TidalDLSettings.from_config(paths["config_file"])
        except ConfigError:
            settings = TidalDLSettings()
        print_welcome(parser, paths, settings)
        return 0

    try:
        if command in OFFLINE or command == "login":
            settings = TidalDLSettings.from_config(paths["config_file"]) if os.path.exists(
                paths["config_file"]
            ) else TidalDLSettings()
            kind = OFFLINE.get(command, "login")
            if kind == "login":
                return await cmd_login(args, settings, paths)
            if kind == "logout":
                return cmd_logout(paths, settings)
            if kind == "stats":
                return cmd_stats(paths)
            if kind == "config":
                return await cmd_config(args, paths)
            from tidal_dl import doctor
            from tidal_dl.library_cmd import cmd_library, cmd_scan

            directory = os.path.expanduser(doctor.resolve_directory(paths["config_file"]) or settings.directory)
            if kind == "doctor":
                results = doctor.run_checks(
                    config_file=paths["config_file"], downloads_db=paths["tidal_db"],
                    library_db=paths["library_db"], directory=directory,
                    credentials_file=paths["credentials_file"],
                )
                if getattr(args, "json", False):
                    print(json.dumps(doctor.to_json(results), ensure_ascii=False, indent=2))
                    return 1 if any(r.level == doctor.FAIL for r in results) else 0
                return doctor.render(results)
            if not settings.write_sentinel:
                args.no_sentinel = True
            if kind == "scan":
                return await cmd_scan(
                    args, directory=directory, quality=settings.quality,
                    downloads_db=None if settings.no_database else paths["tidal_db"],
                )
            return await cmd_library(args, directory=directory)

        settings = _load_settings(paths, args)
        if command in DOWNLOAD_CMDS or command in ("user", "me"):
            return await run_online(command, args, settings, paths)
    except ConfigError as exc:
        ui.error(f"Configuração inválida: {exc}")
        return 2
    except AuthenticationError as exc:
        ui.error(str(exc))
        return 1
    except NetworkError as exc:
        ui.error(f"Falha de rede: {exc}")
        return 1
    except TidalDLException as exc:
        ui.error(str(exc))
        return 1
    parser.print_help()
    return 2


def main() -> None:
    try:
        code = asyncio.run(async_main())
    except KeyboardInterrupt:
        ui.warn("\nInterrompido. Rode o mesmo comando de novo para retomar.")
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
