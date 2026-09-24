"""Definição dos argumentos de linha de comando (argparse).

Mesma organização do commands.py do qobuz-dl-ultra: uma função por
subcomando e um ``build_parser()`` que monta tudo. Argumentos de download
têm ``default=None`` de propósito: só o que o usuário digitar sobrepõe o
config.ini (ver ``TidalDLSettings.apply_args``).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys

from tidal_dl import __version__
from tidal_dl.color import BG, INFO as CYAN, OFF
from tidal_dl.constants import QUALITY_LABELS

LIBRARY_ACTIONS = ["status", "missing", "list", "history", "reconcile", "reset-stuck", "unmark"]
SEARCH_TYPES = ["album", "track", "artist", "playlist"]


class CustomHelpFormatter(argparse.RawTextHelpFormatter):
    """Largura do texto de ajuda acompanha o terminal (iPhone em pé x iPad)."""

    def __init__(self, prog, indent_increment=2, max_help_position=50, width=None):
        try:
            term_width = shutil.get_terminal_size((100, 24)).columns
        except Exception:
            term_width = 100
        max_pos = min(50, max(24, int(term_width * 0.4)))
        super().__init__(prog, indent_increment, max_pos, term_width)


class ColoredArgumentParser(argparse.ArgumentParser):
    """Traduz os textos padrão do argparse para português e colore títulos/flags."""

    def __init__(self, *args, **kwargs):
        kwargs["formatter_class"] = CustomHelpFormatter
        super().__init__(*args, **kwargs)

    def print_help(self, file=None):
        text = self.format_help()
        for en, pt in (
            ("positional arguments:", "argumentos posicionais:"),
            ("optional arguments:", "opções:"),
            ("options:", "opções:"),
            ("show this help message and exit", "mostra esta mensagem de ajuda e sai"),
            ("usage:", "Uso:"),
        ):
            text = text.replace(en, pt)
        text = re.sub(r"^([\w][\w\s]*:)$", f"\n{CYAN}{BG}\\1{OFF}", text, flags=re.MULTILINE)
        text = re.sub(
            r"^(\s+)(-[^\n]{2,}?)( {2,}.*|)$",
            lambda m: f"{m.group(1)}{CYAN}{m.group(2)}{OFF}{m.group(3)}",
            text,
            flags=re.MULTILINE,
        )
        (file or sys.stdout).write(text.lstrip("\n"))


def _quality_help() -> str:
    return "qualidade: " + "; ".join(f"{k}={v}" for k, v in QUALITY_LABELS.items())


def add_common_arg(p: argparse.ArgumentParser) -> None:
    """Opções compartilhadas por todo comando que baixa."""
    p.add_argument("-d", "--directory", metavar="PASTA", default=None, help="pasta de downloads")
    p.add_argument("-q", "--quality", metavar="N", type=int, choices=range(5), default=None,
                   help=_quality_help())
    p.add_argument("--video-directory", metavar="PASTA", default=None,
                   help="pasta de downloads de vídeo (padrão: TidalVideos)")
    p.add_argument("--video-quality", choices=["low", "medium", "high", "LOW", "MEDIUM", "HIGH"],
                   default=None, help="qualidade de vídeo (padrão: high)")
    p.add_argument("-ff", "--folder-format", metavar="FMT", default=None, help="formato do nome da pasta")
    p.add_argument("-tf", "--track-format", metavar="FMT", default=None, help="formato do nome da faixa")
    p.add_argument("--max-workers", "--concurrency", dest="max_workers", type=int, metavar="N", default=None,
                   help="downloads paralelos (padrão: 1 = sequencial, com barra de progresso)")
    p.add_argument("--delay", type=float, metavar="SEG", default=None,
                   help="pausa entre faixas (força modo sequencial: Safety Delay)")
    p.add_argument("--no-progress", action="store_true", default=False,
                   help="sem barra de progresso (útil em logs/cron)")
    p.add_argument("--remux", choices=["auto", "python", "ffmpeg", "none"], default=None,
                   help="como converter o DASH em FLAC (auto = Python puro, ffmpeg como plano B)")
    p.add_argument("--no-db", action="store_true", default=False, help="ignora o banco de dedup")
    p.add_argument("--no-sentinel", action="store_true", default=False,
                   help="não grava .streamrip.json nas pastas")
    p.add_argument("--no-lyrics", action="store_true", default=False, help="não busca letras")
    p.add_argument("--no-lyrics-fallback", action="store_true", default=False,
                   help="não completa letras faltantes no Tidal usando o LRCLIB")
    p.add_argument("--no-cover", action="store_true", default=False, help="não baixa/embute capa")
    p.add_argument("--no-fallback", action="store_true", default=False,
                   help="falha em vez de baixar em qualidade menor quando a pedida não existir")


def login_args(sub):
    p = sub.add_parser("login", usage="tidal-dl login [--device]", help="entra na sua conta Tidal",
                       description="Login PKCE (Lossless/Hi-Res). Use --device só se não puder "
                                   "colar uma URL (limitado a AAC).")
    p.add_argument("--device", action="store_true", default=False,
                   help="login por código de dispositivo (AAC apenas)")
    return p


def logout_args(sub):
    return sub.add_parser("logout", help="apaga o token salvo")


def user_args(sub):
    return sub.add_parser("user", aliases=["me"], help="mostra conta, país e assinatura")


def dl_args(sub):
    p = sub.add_parser(
        "dl", usage="tidal-dl dl [opções] ITEM [ITEM...]", help="baixa por URL ou arquivo .txt",
        description="Baixa álbuns, faixas, playlists, artistas e vídeos musicais a partir de URLs "
                    "do Tidal (ou de um .txt com uma URL por linha).",
    )
    p.add_argument("SOURCE", nargs="+", help="URL(s) do Tidal ou caminho de um .txt")
    p.add_argument("--eps", action="store_true", default=False,
                   help="em URL de artista, inclui EPs e singles")
    add_common_arg(p)
    return p


def search_args(sub):
    p = sub.add_parser("search", aliases=["fun", "i"], usage="tidal-dl search [-t TIPO] [TERMO...]",
                       help="busca e escolhe o que baixar",
                       description="Busca no catálogo e deixa escolher numa tabela. Sem TERMO, abre "
                                    "o modo interativo completo (escolhe o tipo, depois o termo, com "
                                    "opção de favoritos) -- estilo qobuz-dl-ultra.")
    p.add_argument("QUERY", nargs="*", help="termo de busca (omita pra abrir o modo interativo completo)")
    p.add_argument("-t", "--type", choices=SEARCH_TYPES, default="album", help="tipo (padrão: album)")
    add_common_arg(p)
    return p


def lucky_args(sub):
    p = sub.add_parser("lucky", usage="tidal-dl lucky [-t TIPO] [-n N] TERMO...",
                       help="baixa direto o(s) primeiro(s) resultado(s)")
    p.add_argument("QUERY", nargs="+")
    p.add_argument("-t", "--type", choices=["album", "track", "artist", "playlist"], default="album")
    p.add_argument("-n", "--number", type=int, default=1, help="quantos resultados (padrão 1)")
    add_common_arg(p)
    return p


def sync_favorites_args(sub):
    p = sub.add_parser(
        "sync-favorites", aliases=["sf"], usage="tidal-dl sync-favorites [opções]",
        help="sincroniza favoritos da conta com o catálogo local",
        description="Compara seus álbuns favoritos com o catálogo local (library.db): mostra "
                    "novos/removidos e, opcionalmente, baixa o que falta. Na primeira vez rode "
                    "`tidal-dl scan` antes, para marcar o que você já tem.",
    )
    p.add_argument("--download-new", action="store_true", default=False,
                   help="baixa os favoritados desde a última sincronização")
    p.add_argument("--download-missing", action="store_true", default=False,
                   help="baixa TODOS os favoritos ainda não completos no disco")
    p.add_argument("--limit", type=int, metavar="N", default=None, help="máximo de álbuns por execução")
    p.add_argument("--every", type=int, metavar="MIN", default=None,
                   help="repete a cada MIN minutos (modo contínuo)")
    p.add_argument("--dry-run", action="store_true", default=False, help="só mostra o diff")
    p.add_argument("--yes", "-y", action="store_true", default=False, help="não pede confirmação")
    add_common_arg(p)
    return p


def scan_args(sub):
    p = sub.add_parser(
        "scan", aliases=["library-scan"], usage="tidal-dl scan [opções] [DIR]",
        help="reconcilia sua pasta de música com o catálogo",
        description="Casa cada álbum do disco com o catálogo (tag TIDALALBUMID, UPC, nome, fuzzy). "
                    "Funciona offline.",
    )
    p.add_argument("DIR", nargs="?", default=None, help="pasta a varrer (padrão: diretório de downloads)")
    p.add_argument("--dry-run", action="store_true", default=False, help="classifica sem gravar")
    p.add_argument("--no-sentinel", action="store_true", default=False, help="não grava .streamrip.json")
    p.add_argument("--no-adopt", action="store_true", default=False,
                   help="não adota pastas com tag de ID que não estão nos favoritos")
    p.add_argument("--no-review", action="store_true", default=False, help="não pergunta sobre dúvidas")
    p.add_argument("--rescan", action="store_true", default=False, help="reexamina pastas com sentinela")
    p.add_argument("--max-depth", type=int, default=4, metavar="N")
    p.add_argument("--fuzzy-threshold", type=float, default=0.85, metavar="0-1")
    p.add_argument("--json", metavar="ARQUIVO", default=None, help="salva o relatório em JSON")
    return p


def library_args(sub):
    p = sub.add_parser("library", aliases=["lib"], usage="tidal-dl library [ação]",
                       help="status e manutenção do catálogo local",
                       description="Ações: " + ", ".join(LIBRARY_ACTIONS))
    p.add_argument("action", nargs="?", default="status", choices=LIBRARY_ACTIONS, metavar="ação")
    p.add_argument("TARGET", nargs="?", default=None, help="pasta (reconcile) ou ID do álbum (unmark)")
    p.add_argument("--limit", type=int, default=None, metavar="N")
    p.add_argument("--fix", action="store_true", default=False)
    p.add_argument("--dry-run", action="store_true", default=False)
    return p


def doctor_args(sub):
    p = sub.add_parser("doctor", aliases=["check"], help="diagnostica a instalação (somente leitura)")
    p.add_argument("--json", action="store_true", default=False)
    return p


def stats_args(sub):
    return sub.add_parser("stats", help="estatísticas dos seus downloads")


def config_args(sub):
    p = sub.add_parser("config", help="assistente de configuração (config.ini)")
    p.add_argument("--show", action="store_true", default=False, help="só mostra a configuração atual")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = ColoredArgumentParser(
        prog="tidal-dl",
        description="tidal-dl-ultra: downloader de terminal para o Tidal (Lossless/Hi-Res).",
    )
    parser.add_argument("--version", action="version", version=f"tidal-dl-ultra {__version__}",
                        help="mostra a versão e sai")
    parser.add_argument("-r", "--reset", action="store_true", default=False,
                        help="cria/reseta o arquivo de configuração (assistente interativo)")
    parser.add_argument("-p", "--purge", action="store_true", default=False,
                        help="apaga o banco de dados de downloads-já-feitos (tidal_dl.db)")
    parser.add_argument("--quiet", action="store_true", default=False, help="só erros")
    parser.add_argument("--verbose", action="store_true", default=False, help="mensagens de depuração")
    parser.add_argument("--no-color", action="store_true", default=False, help="sem cores")
    sub = parser.add_subparsers(dest="command", metavar="comando", title="comandos")
    for fn in (login_args, logout_args, user_args, dl_args, search_args, lucky_args,
               sync_favorites_args, scan_args, library_args, doctor_args, stats_args, config_args):
        fn(sub)
    return parser
