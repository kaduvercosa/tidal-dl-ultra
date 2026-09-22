"""Tela inicial (``tidal-dl`` sem argumentos): logo, sessão, comandos e flags.

Mesmo desenho do ``_print_welcome_screen`` do qobuz-dl-ultra (logo em blocos
que se adapta à largura do terminal, comandos e flags extraídos do próprio
argparse), com um bloco extra de SESSÃO e de PRIMEIROS PASSOS: quem abre o
programa pela primeira vez no a-Shell vê na hora se está logado e o que
digitar em seguida. Só lê arquivos locais (nada de rede).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

from tidal_dl import __version__, db as dbm, ui
from tidal_dl.color import BG, INFO as CYAN, MUTED, OFF, RESET
from tidal_dl.color import HIGHLIGHT as ACCENT
from tidal_dl.constants import QUALITY_LABELS

_LOGO_FONT = {
    "A": ["01110", "10001", "11111", "10001", "10001"],
    "D": ["11110", "10001", "10001", "10001", "11110"],
    "I": ["11111", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "11111"],
    "R": ["11110", "10001", "11110", "10100", "10010"],
    "T": ["11111", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "01110"],
    "-": ["00000", "00000", "11111", "00000", "00000"],
}
_BLOCK = "\u2588"

COMMAND_DESCRIPTIONS_PT = {
    "login": "Entra na sua conta Tidal (PKCE: Lossless/Hi-Res). Use --device só se não puder colar uma URL (AAC).",
    "logout": "Apaga o token salvo neste aparelho.",
    "user": "Mostra conta, país, plano de assinatura e a qualidade máxima liberada.",
    "dl": "Baixa por URL de álbum, faixa, playlist ou artista do Tidal, ou um .txt com uma lista de URLs.",
    "search": "Busca interativa: procura álbuns/faixas/artistas/playlists e escolhe o que baixar por número.",
    "lucky": "Baixa os N primeiros resultados de uma busca, sem passar URL.",
    "sync-favorites": "Sincroniza seus álbuns favoritos com o catálogo local (novos/removidos) e baixa o que falta.",
    "scan": "Casa sua pasta de música com o catálogo (tag de ID, UPC, nome, fuzzy) e grava sentinelas.",
    "library": "Status e manutenção do catálogo local (missing, history, reconcile, reset-stuck).",
    "doctor": "Diagnostica ambiente, config, login, bancos e sentinelas (somente leitura).",
    "stats": "Mostra estatísticas dos seus downloads.",
    "config": "Assistente de configuração (config.ini): pasta, qualidade, formatos.",
}

FLAG_DESCRIPTIONS_PT = {
    "quiet": "mostra só erros",
    "verbose": "mensagens de depuração",
    "no_color": "desliga as cores",
    "version": "mostra a versão e sai",
}


def _render_word(word: str) -> list[str]:
    rows = ["" for _ in range(5)]
    for i, ch in enumerate(word):
        glyph = _LOGO_FONT[ch]
        for r in range(5):
            rows[r] += "".join(_BLOCK if px == "1" else " " for px in glyph[r])
            if i != len(word) - 1:
                rows[r] += " "
    return rows


def print_logo(cols: int) -> None:
    """Logo TIDAL-DL / ULTRA; em tela estreita (iPhone em pé) cai para TDL."""
    line1, line2 = _render_word("TIDAL-DL"), _render_word("ULTRA")
    width = len(line1[0])
    if cols >= width + 5:
        for word in (line1, line2):
            pad = " " * max((cols - len(word[0])) // 2, 0)
            for row in word:
                ui.emit(f"{CYAN}{pad}{row}{OFF}")
    else:
        short = _render_word("TDL")
        pad = " " * max((cols - len(short[0])) // 2, 0)
        for row in short:
            ui.emit(f"{CYAN}{pad}{row}{OFF}")


def extract_subcommands(parser: argparse.ArgumentParser) -> list[tuple[str, Optional[str], str]]:
    action = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if action is None:
        return []
    out = []
    for choice in action._choices_actions:
        primary = choice.dest
        sp = action.choices[primary]
        aliases = [n for n, p in action.choices.items() if p is sp and n != primary]
        out.append((primary, ", ".join(aliases) or None, choice.help or ""))
    return out


def extract_global_flags(parser: argparse.ArgumentParser) -> list[tuple[str, str, str]]:
    out = []
    for a in parser._actions:
        if isinstance(a, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        out.append((", ".join(a.option_strings), a.dest, a.help or ""))
    return out


def program_name() -> str:
    """``tidal-dl`` quando instalado; ``python3 -m tidal_dl`` quando rodado da pasta."""
    argv0 = os.path.basename(sys.argv[0] or "")
    return "python3 -m tidal_dl" if argv0 in ("__main__.py", "-m", "", "-c") else "tidal-dl"


def session_lines(paths: dict, settings) -> list[tuple[str, str, bool]]:
    """``(rótulo, valor, ok)`` da sessão -- só arquivos locais."""
    from tidal_dl.auth import CredentialStore

    rows: list[tuple[str, str, bool]] = []
    creds = CredentialStore(paths["credentials_file"], use_keyring=not settings.disable_keyring).load()
    if creds is None:
        rows.append(("Login", "não logado", False))
    else:
        left = creds.token_expiry - time.time()
        if left > 0:
            tok = f"token válido por {int(left // 3600)}h"
        elif creds.refresh_token:
            tok = "renova sozinho"
        else:
            tok = "token expirado"
        method = "PKCE" if creds.auth_method == "pkce" else "dispositivo (só AAC)"
        rows.append(("Login", f"{method} · país {creds.country_code} · {tok}", creds.auth_method == "pkce"))
    rows.append(("Qualidade", f"{settings.quality} = {QUALITY_LABELS.get(settings.quality, '?')}", True))
    rows.append(("Pasta", settings.directory, True))
    total = dbm.get_stats(paths["tidal_db"])["total"]
    rows.append(("Baixados", f"{total} registro(s)" if total else "nada ainda", True))
    return rows


def print_welcome(parser: argparse.ArgumentParser, paths: dict, settings) -> None:
    cols = ui.width()
    prog = program_name()
    ui.blank()
    print_logo(cols)
    version = f"v{__version__}"
    ui.emit(f"{RESET}{' ' * max((cols - len(version)) // 2, 0)}{version}\n")

    ui.rule("=")
    ui.emit(f"{ACCENT}{BG}Uso: {prog} <comando>{OFF}")
    ui.wrapped(f"{ACCENT}Help:{RESET} {prog} <comando> --help {MUTED}(todas as opções do comando){OFF}", indent=2)
    ui.blank()

    ui.emit(f"{ACCENT}{BG}SESSÃO:{OFF}\n")
    logged = True
    for label, value, ok in session_lines(paths, settings):
        if label == "Login":
            logged = ok and "não logado" not in value
        ui.wrapped(f"{ACCENT}{label}:{OFF} {value}", indent=2)
    ui.blank()

    if not logged:
        ui.emit(f"{ACCENT}{BG}PRIMEIROS PASSOS:{OFF}\n")
        ui.wrapped(f"1) {ACCENT}{prog} login{OFF}  {MUTED}(entra na conta){OFF}", indent=2)
        ui.wrapped(f"2) {ACCENT}{prog} dl <URL do Tidal>{OFF}  {MUTED}(baixa){OFF}", indent=2)
        ui.wrapped(f"3) {ACCENT}{prog} doctor{OFF}  {MUTED}(se algo não funcionar){OFF}", indent=2)
        ui.blank()

    ui.emit(f"{ACCENT}{BG}COMANDOS:{OFF}\n")
    for name, aliases, help_text in extract_subcommands(parser):
        label = name if not aliases else f"{name} ({aliases})"
        ui.wrapped(f"{ACCENT}{label}{OFF}", indent=2)
        ui.wrapped(COMMAND_DESCRIPTIONS_PT.get(name, help_text), indent=4)
    ui.blank()

    ui.emit(f"{ACCENT}{BG}FLAGS GLOBAIS:{RESET} {MUTED}(valem para qualquer comando){OFF}\n" if cols >= 62
            else f"{BG}FLAGS GLOBAIS:{RESET}\n")
    for flag_str, dest, help_text in extract_global_flags(parser):
        ui.emit(f"  {ACCENT}{flag_str}{OFF}")
        ui.wrapped(FLAG_DESCRIPTIONS_PT.get(dest, help_text), indent=4)
    ui.blank()
    ui.rule("=")
