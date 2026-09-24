"""Ponto de entrada do tidal-dl-ultra (``tidal-dl``).

Comandos que NÃO precisam de login (doctor, scan, library, stats, config,
logout) rodam mesmo com token expirado ou config quebrado -- justamente
quando são necessários. Os demais criam ``TidalDL`` e renovam o token sozinhos.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

from tidal_dl import db as dbm, ui
from tidal_dl.auth import CredentialStore, login_device, login_pkce
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
DOWNLOAD_CMDS = {"dl", "search", "fun", "i", "lucky"} | SYNC


def _load_settings(paths: dict, args) -> TidalDLSettings:
    settings = TidalDLSettings.from_config(paths["config_file"])
    return settings.apply_args(args)


# ---------------------------------------------------------------------------
# login / user / logout
# ---------------------------------------------------------------------------


async def cmd_login(args, settings: TidalDLSettings, paths: dict, http=None) -> int:
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
    st.directory = os.path.expanduser(await _prompt("Pasta de downloads", st.directory))
    for k, v in QUALITY_LABELS.items():
        ui.detail(f"{k} = {v}", indent=2)
    while True:
        q = await _prompt("Qualidade (0-4)", str(st.quality))
        if q.isdigit() and int(q) in QUALITY_LABELS:
            st.quality = int(q)
            break
        ui.warn("Digite um número de 0 a 4.")
    st.folder_format = await _prompt("Formato da pasta", st.folder_format)
    st.track_format = await _prompt("Formato da faixa", st.track_format)
    st.video_directory = os.path.expanduser(await _prompt("Pasta de downloads de vídeo", st.video_directory))
    while True:
        vq = (await _prompt("Qualidade de vídeo (low/medium/high)", st.video_quality.lower())).strip().upper()
        if vq in ("LOW", "MEDIUM", "HIGH"):
            st.video_quality = vq
            break
        ui.warn("Digite low, medium ou high.")
    ui.detail("1 = sequencial (com barra de progresso); mais que 1 = paralelo (sem barras).", indent=2)
    st.max_workers = int(await _prompt("Downloads paralelos (1-16)", str(st.max_workers)) or st.max_workers)
    lyr = await _prompt("Baixar letras? (s/n)", "s" if st.lyrics else "n")
    st.lyrics = lyr.lower().startswith(("s", "y"))
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
            return 1 if summary["failed"] else 0
        if command in ("search", "fun", "i"):
            if args.QUERY:
                ok = await tidal.interactive(" ".join(args.QUERY), args.type)
            else:
                # Sem TERMO: abre o fluxo completo -- primeiro escolhe o tipo
                # (ou "Favoritos"), depois pergunta o termo, só então mostra
                # a tabela. Com TERMO já dado na linha de comando, continua
                # indo direto pra busca (uso não-interativo/scripts).
                ok = await tidal.interactive_menu()
            return 0 if ok else 1
        if command == "lucky":
            return 0 if await tidal.lucky(" ".join(args.QUERY), args.type, args.number) else 1
        if command in SYNC:
            from tidal_dl.library_cmd import cmd_sync_favorites

            return await cmd_sync_favorites(args, tidal)
        raise TidalDLException(f"comando desconhecido: {command}")
    finally:
        await tidal.aclose()
        for root in {settings.directory, settings.video_directory}:
            if os.path.isdir(root):
                clean_leftovers(root, TMP_PREFIX)


async def async_main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ui.configure(quiet=args.quiet, verbose=args.verbose, color=not args.no_color)
    ui.install_logging()

    command = args.command
    paths = get_config_paths()

    # `-r`/`--reset`: (re)cria o config.ini pelo assistente e sai. Também roda
    # sozinho na primeira vez (config.ini ainda não existe) -- a MENOS que
    # `-r`/`--reset` já tenha sido pedido explicitamente, para não rodar 2x.
    first_run = not os.path.isfile(paths["config_file"])
    if getattr(args, "reset", False) or (first_run and command):
        import argparse as _argparse

        rc = await cmd_config(_argparse.Namespace(show=False), paths)
        if getattr(args, "reset", False):
            if rc == 0:
                # Reset deu certo: cai direto na página inicial (mesmo texto
                # que apareceria rodando `tidal-dl` sem argumentos), em vez
                # de só voltar pro shell -- evita ter que rodar o comando de
                # novo pra ver os próximos passos.
                from tidal_dl.welcome import print_welcome

                try:
                    settings = TidalDLSettings.from_config(paths["config_file"])
                except ConfigError:
                    settings = TidalDLSettings()
                print_welcome(parser, paths, settings)
            return rc

    # `-p`/`--purge`: apaga o banco de downloads-já-feitos e sai (sem pedir
    # confirmação, igual ao qobuz-dl-ultra -- é reversível, só perde o dedup).
    if getattr(args, "purge", False):
        try:
            os.remove(paths["tidal_db"])
            ui.ok("O banco de dados de downloads foi apagado.")
        except FileNotFoundError:
            ui.info("Não havia banco de dados para apagar.")
        return 0

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
