"""Orquestração: login -> cliente -> URLs/busca -> downloader.

Espelha o papel do core.py do qobuz-dl-ultra (classe principal que a CLI
instancia), mas bem menor: o Tidal não exige raspar segredos de bundle.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from typing import Any, Optional

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import get_app
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout.containers import HSplit, ScrollOffsets, Window
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.layout.layout import Layout
    from prompt_toolkit.utils import get_cwidth
except ImportError:  # pragma: no cover - mensagem só aparece sem a dependência instalada
    sys.exit("Erro: por favor instale o prompt_toolkit executando: pip install prompt_toolkit")

from tidal_dl import ui
from tidal_dl.api import TidalAPI
from tidal_dl.auth import CredentialStore, Credentials
from tidal_dl.downloader import Downloader
from tidal_dl.exceptions import (
    AuthenticationError,
    NonStreamable,
    ResourceNotFoundError,
    TidalDLException,
)
from tidal_dl.interactive_ui import _align_text, _get_table_layout, pt_style, prompt_style
from tidal_dl.models import Album, Artist, Playlist, Track
from tidal_dl.net import HttpClient, create_client
from tidal_dl.settings import TidalDLSettings
from tidal_dl.utils import get_config_paths, human_duration, parse_url

logger = logging.getLogger(__name__)

SEARCH_KINDS = {"album": "albums", "track": "tracks", "artist": "artists", "playlist": "playlists"}

# Rótulo curto pra coluna QUALIDADE da TUI. "24b"/"24bit" aciona o destaque
# dourado em _tui_select (mesma regra do qobuz-dl-ultra: `"24b" in ql`).
QUALITY_SHORT = {
    "LOW": "AAC ~96k", "HIGH": "AAC 320k", "LOSSLESS": "FLAC 16bit", "HI_RES_LOSSLESS": "FLAC 24bit",
}


# ==============================================================================
# TUI de seleção em tela cheia (prompt_toolkit) -- portada do qobuz-dl-ultra
# (qobuz_dl/core.py::_tui_select) praticamente linha a linha, só trocando o
# import do tema (agora tidal_dl.interactive_ui) e os nomes de import no
# topo. Usada pelo modo interativo (`interactive()` abaixo) no lugar do
# menu por número + input() de texto que havia antes.
# ==============================================================================
async def _tui_select(title, options_dicts, is_multi=False, item_category="album"):
    """Tela de seleção em tela cheia (prompt_toolkit), usada pelo modo
    interativo (busca de álbum/faixa/artista/playlist).

    Args:
        title: texto do cabeçalho.
        options_dicts: lista de dicts ``{"id": ..., "meta": {...}}`` (ver
            ``TidalDL._search_option``), ou lista de strings simples pra
            menus tipo "filter".
        is_multi: se True, permite selecionar vários com [espaço] e
            confirmar com [Enter]; se False, [Enter] já seleciona o item
            sob o cursor.
        item_category: "album" | "track" | "playlist" | "artist" | "filter".

    Atalhos: ↑/↓ ou j/k move; PageUp/PageDown pula 10; g/G vai pro
    primeiro/último; 1-9 pula direto pra posição; r força redesenho;
    espaço/t (multi) marca um/todos; Enter confirma; Ctrl+C/Esc cancela.

    Retorna:
        - is_multi=True:  lista de tuplas (item, índice_original)
        - is_multi=False: tupla única (item, índice_original)
        - Ctrl+C/Esc: propaga KeyboardInterrupt (capturado no chamador)
    """
    bindings = KeyBindings()
    selected_indices = set()
    cursor_pos = 0

    def _move_up(event):
        nonlocal cursor_pos
        cursor_pos = max(0, cursor_pos - 1)

    def _move_down(event):
        nonlocal cursor_pos
        if options_dicts:
            cursor_pos = min(len(options_dicts) - 1, cursor_pos + 1)

    bindings.add("up")(_move_up)
    bindings.add("k")(_move_up)
    bindings.add("down")(_move_down)
    bindings.add("j")(_move_down)

    _PAGE_JUMP = 10

    @bindings.add("pageup")
    def _(event):
        nonlocal cursor_pos
        cursor_pos = max(0, cursor_pos - _PAGE_JUMP)

    @bindings.add("pagedown")
    def _(event):
        nonlocal cursor_pos
        if options_dicts:
            cursor_pos = min(len(options_dicts) - 1, cursor_pos + _PAGE_JUMP)

    @bindings.add("g")
    def _(event):
        nonlocal cursor_pos
        cursor_pos = 0

    @bindings.add("G")
    def _(event):
        nonlocal cursor_pos
        if options_dicts:
            cursor_pos = len(options_dicts) - 1

    def _make_digit_jump(n):
        def _jump(event):
            nonlocal cursor_pos
            idx = n - 1
            if options_dicts and idx < len(options_dicts):
                cursor_pos = idx

        return _jump

    for _digit in range(1, 10):
        bindings.add(str(_digit))(_make_digit_jump(_digit))

    @bindings.add("r")
    def _(event):
        event.app.invalidate()

    if is_multi:

        @bindings.add("space")
        def _(event):
            if not options_dicts:
                return
            if cursor_pos in selected_indices:
                selected_indices.remove(cursor_pos)
            else:
                selected_indices.add(cursor_pos)

        @bindings.add("t")
        def _(event):
            if not options_dicts:
                return
            if len(selected_indices) == len(options_dicts):
                selected_indices.clear()
            else:
                selected_indices.update(range(len(options_dicts)))

    @bindings.add("enter")
    def _(event):
        if not options_dicts:
            return
        if is_multi:
            if not selected_indices:
                selected_indices.add(cursor_pos)
            event.app.exit(
                result=[(options_dicts[i], i) for i in sorted(list(selected_indices))]
            )
        else:
            event.app.exit(result=(options_dicts[cursor_pos], cursor_pos))

    @bindings.add("c-c")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt)

    @bindings.add("escape")
    def _(event):
        event.app.exit(exception=KeyboardInterrupt)

    def get_header_text():
        try:
            columns = get_app().output.get_size().columns
        except Exception:
            columns, _ = shutil.get_terminal_size((80, 24))

        is_table, widths, headers, borders = _get_table_layout(columns, is_multi, item_category)

        prefix_len = 5 if is_multi else 3
        hdr_pref = " " * prefix_len

        res = [("class:title", f"\n === {title} ===\n\n")]

        if is_table:
            res.append(("class:meta", hdr_pref + borders["top"] + "\n"))
            res.append(("class:meta", hdr_pref + "│ "))
            for idx, (h, w) in enumerate(zip(headers, widths)):
                res.append(("class:table_header", _align_text(h, w)))
                if idx < len(headers) - 1:
                    res.append(("class:meta", " │ "))
                else:
                    res.append(("class:meta", " │\n"))
            res.append(("class:meta", hdr_pref + borders["mid"]))

        return res

    def get_list_text():
        try:
            columns = get_app().output.get_size().columns
        except Exception:
            columns, _ = shutil.get_terminal_size((80, 24))

        is_table, widths, headers, borders = _get_table_layout(columns, is_multi, item_category)
        res = []

        def add_line(fragments, fill_bg=False):
            final_fragments = []
            current_w = 0
            for st, txt in fragments:
                txt_w = get_cwidth(txt)
                if current_w + txt_w > columns:
                    allowed = max(0, columns - current_w)
                    trunc_txt = ""
                    temp_w = 0
                    for char in txt:
                        cw = get_cwidth(char)
                        if temp_w + cw > allowed:
                            break
                        trunc_txt += char
                        temp_w += cw
                    if fill_bg:
                        final_fragments.append(("class:hovered", trunc_txt))
                    else:
                        final_fragments.append((st, trunc_txt))
                    current_w += temp_w
                    break
                else:
                    if fill_bg:
                        final_fragments.append(("class:hovered", txt))
                    else:
                        final_fragments.append((st, txt))
                    current_w += txt_w

            padding = max(0, columns - current_w)
            if padding > 0:
                pad_st = "class:hovered" if fill_bg else ""
                final_fragments.append((pad_st, " " * padding))

            final_fragments.append(("", "\n"))
            res.extend(final_fragments)

        inner_w = max(10, columns - 6)

        def add_card_line(fragments, hovered=False, is_checked=False, border_style="class:meta"):
            final_fragments = []
            current_w = 0
            row_st = "class:hovered" if hovered else ("class:highlight" if is_checked else "")
            border_st = "class:hovered" if hovered else border_style
            vt = "┃" if is_checked else "│"
            final_fragments.append((border_st, f" {vt} "))

            for st, txt in fragments:
                txt_w = get_cwidth(txt)
                if current_w + txt_w > inner_w:
                    allowed = max(0, inner_w - current_w)
                    trunc_txt = ""
                    temp_w = 0
                    for char in txt:
                        cw = get_cwidth(char)
                        if temp_w + cw > allowed:
                            break
                        trunc_txt += char
                        temp_w += cw
                    st_use = "class:hovered" if hovered else ("class:highlight" if is_checked else st)
                    final_fragments.append((st_use, trunc_txt))
                    current_w += temp_w
                    break
                else:
                    st_use = "class:hovered" if hovered else ("class:highlight" if is_checked else st)
                    final_fragments.append((st_use, txt))
                    current_w += txt_w

            padding = max(0, inner_w - current_w)
            if padding > 0:
                pad_st = "class:hovered" if hovered else ("class:highlight" if is_checked else "")
                final_fragments.append((pad_st, " " * padding))

            final_fragments.append((border_st, f" {vt}\n"))
            res.extend(final_fragments)

        for i, opt in enumerate(options_dicts):
            hovered = i == cursor_pos
            checked = i in selected_indices

            if hovered:
                res.append(("[SetCursorPosition]", ""))

            style = "class:highlight" if checked else ""
            title_style = "class:highlight"

            ptr = "➤" if hovered else " "
            if is_multi:
                chk = "*" if checked else "◇"
                prefix = f" {ptr} {chk} "
            else:
                prefix = f" {ptr} "

            row_st = "class:hovered" if hovered else style
            tit_st = "class:hovered" if hovered else ("class:highlight" if checked else "class:item_title")
            border_st = "class:hovered" if hovered else ("class:highlight" if checked else "class:meta")

            if not is_table:
                top_l = "┏" if checked else "╭"
                top_r = "┓" if checked else "╮"
                hz = "━" if checked else "─"
                res.append((border_st, f" {top_l}{hz * (inner_w + 2)}{top_r}\n"))

            if item_category == "filter" and isinstance(opt, str):
                if is_table:
                    add_line([(tit_st, f"{prefix}{opt}")], fill_bg=hovered)
                else:
                    add_card_line([(tit_st, f"{prefix}{opt}")], hovered=hovered, is_checked=checked, border_style=border_st)

            elif isinstance(opt, str):
                if is_table:
                    add_line([(row_st, f"{prefix}{opt}")], fill_bg=hovered)
                else:
                    add_card_line([(row_st, f"{prefix}{opt}")], hovered=hovered, is_checked=checked, border_style=border_st)

            else:
                meta = opt.get("meta", {})
                ql = meta.get("quality", "")
                typ = meta.get("type", "")

                ql_color = "fg:#c59b27 bold" if "24b" in ql else "fg:#5fa8d3"
                ql_st = "class:hovered" if hovered else ("class:highlight" if checked else ql_color)

                raw_typ = typ.strip().lower()
                if raw_typ == "album":
                    typ_st = "class:type_album"
                elif raw_typ == "ep":
                    typ_st = "class:type_ep"
                elif raw_typ == "single":
                    typ_st = "class:type_single"
                elif raw_typ in ("track", "faixa"):
                    typ_st = "class:type_track"
                elif raw_typ == "compilation":
                    typ_st = "class:type_comp"
                else:
                    typ_st = "class:type_other"

                if hovered:
                    typ_st = "class:hovered"
                elif checked:
                    typ_st = "class:highlight"

                if item_category == "album":
                    tit_str = meta.get("title", "")
                    art = meta.get("artist", "")
                    yr = meta.get("year", "")
                    fx = str(meta.get("tracks_count", ""))

                    if is_table:
                        tit_align = _align_text(tit_str, widths[0])
                        art_align = _align_text(art, widths[1])
                        typ_align = _align_text(typ, widths[2])
                        yr_align = _align_text(yr, widths[3])
                        fx_align = _align_text(fx, widths[4])
                        ql_align = _align_text(ql, widths[5])

                        if hovered:
                            p1 = f"│ {tit_align} │ {art_align} │ {typ_align} │ {yr_align} │ {fx_align} │ "
                            add_line([(style, prefix), (row_st, p1), (ql_st, ql_align), (row_st, " │")], fill_bg=False)
                        else:
                            add_line(
                                [
                                    (style, prefix), (style, "│ "), (tit_st, tit_align), (style, " │ "),
                                    (style, art_align), (style, " │ "), (typ_st, typ_align), (style, " │ "),
                                    (style, yr_align), (style, " │ "), (style, fx_align), (style, " │ "),
                                    (ql_st, ql_align), (style, " │"),
                                ],
                                fill_bg=False,
                            )
                    else:
                        l1 = [(tit_st, f"{prefix}{tit_str}")]
                        ql_str = f"[{ql}]"
                        pad_len = inner_w - get_cwidth(l1[0][1]) - get_cwidth(ql_str)
                        if pad_len > 0:
                            l1.append(("", " " * pad_len))
                            l1.append((ql_st, ql_str))
                        add_card_line(l1, hovered=hovered, is_checked=checked, border_style=border_st)

                        if hovered:
                            add_card_line([(row_st, f"   👤 {art}")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   💿 {typ} · {yr}")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   🎵 {fx} faixas")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   🎚 {ql}")], hovered=hovered, is_checked=checked, border_style=border_st)
                        else:
                            add_card_line(
                                [(style, f"   👤 {art} · "), (typ_st, typ), (style, f" · {yr}")],
                                hovered=hovered, is_checked=checked, border_style=border_st,
                            )

                elif item_category == "track":
                    tit_str = meta.get("title", "")
                    art = meta.get("artist", "")
                    alb = meta.get("album", "")
                    dur = meta.get("duration", "")

                    if is_table:
                        tit_align = _align_text(tit_str, widths[0])
                        art_align = _align_text(art, widths[1])
                        alb_align = _align_text(alb, widths[2])
                        typ_align = _align_text(typ, widths[3])
                        dur_align = _align_text(dur, widths[4])
                        ql_align = _align_text(ql, widths[5])

                        if hovered:
                            p1 = f"│ {tit_align} │ {art_align} │ {alb_align} │ {typ_align} │ {dur_align} │ "
                            add_line([(style, prefix), (row_st, p1), (ql_st, ql_align), (row_st, " │")], fill_bg=False)
                        else:
                            add_line(
                                [
                                    (style, prefix), (style, "│ "), (tit_st, tit_align), (style, " │ "),
                                    (style, art_align), (style, " │ "), (style, alb_align), (style, " │ "),
                                    (typ_st, typ_align), (style, " │ "), (style, dur_align), (style, " │ "),
                                    (ql_st, ql_align), (style, " │"),
                                ],
                                fill_bg=False,
                            )
                    else:
                        l1 = [(tit_st, f"{prefix}{tit_str}")]
                        ql_str = f"[{ql}]"
                        pad_len = inner_w - get_cwidth(l1[0][1]) - get_cwidth(ql_str)
                        if pad_len > 0:
                            l1.append(("", " " * pad_len))
                            l1.append((ql_st, ql_str))
                        add_card_line(l1, hovered=hovered, is_checked=checked, border_style=border_st)

                        if hovered:
                            add_card_line([(row_st, f"   👤 {art}")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   💿 {alb}")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   📀 {typ} · ⏱ {dur}")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   🎚 {ql}")], hovered=hovered, is_checked=checked, border_style=border_st)
                        else:
                            add_card_line(
                                [(style, f"   👤 {art} · "), (typ_st, typ), (style, f" · ⏱ {dur}")],
                                hovered=hovered, is_checked=checked, border_style=border_st,
                            )

                elif item_category == "playlist":
                    n = meta.get("name", "")
                    o = meta.get("owner", "")
                    c = str(meta.get("count", 0))
                    dur = meta.get("duration", "--:--")

                    if is_table:
                        n_align = _align_text(n, widths[0])
                        o_align = _align_text(o, widths[1])
                        c_align = _align_text(c, widths[2])
                        dur_align = _align_text(dur, widths[3])

                        if hovered:
                            p1 = f"│ {n_align} │ {o_align} │ {c_align} │ {dur_align} │"
                            add_line([(style, prefix), (row_st, p1)], fill_bg=False)
                        else:
                            add_line(
                                [
                                    (style, prefix), (style, "│ "), (tit_st, n_align), (style, " │ "),
                                    (style, o_align), (style, " │ "), (style, c_align), (style, " │ "),
                                    (style, dur_align), (style, " │"),
                                ],
                                fill_bg=False,
                            )
                    else:
                        add_card_line([(tit_st, f"{prefix}{n}")], hovered=hovered, is_checked=checked, border_style=border_st)
                        if hovered:
                            add_card_line([(row_st, f"   👤 {o}")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   🎵 {c} faixas")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, f"   ⏱ {dur}")], hovered=hovered, is_checked=checked, border_style=border_st)
                        else:
                            add_card_line([(style, f"   👤 {o} · 🎵 {c} · ⏱ {dur}")], hovered=hovered, is_checked=checked, border_style=border_st)

                elif item_category == "artist":
                    n = meta.get("name", "")
                    c_str = f"{meta.get('count', '')} lançamentos".strip()

                    if is_table:
                        n_align = _align_text(n, widths[0])
                        c_align = _align_text(c_str, widths[1])

                        if hovered:
                            p1 = f"│ {n_align} │ {c_align} │"
                            add_line([(style, prefix), (row_st, p1)], fill_bg=False)
                        else:
                            add_line(
                                [
                                    (style, prefix), (style, "│ "), (tit_st, n_align), (style, " │ "),
                                    (style, c_align), (style, " │"),
                                ],
                                fill_bg=False,
                            )
                    else:
                        add_card_line([(tit_st, f"{prefix}👤 {n}")], hovered=hovered, is_checked=checked, border_style=border_st)
                        if hovered:
                            add_card_line([(row_st, f"   🎵 {c_str}")], hovered=hovered, is_checked=checked, border_style=border_st)
                            add_card_line([(row_st, "   [Enter] para baixar discografia")], hovered=hovered, is_checked=checked, border_style=border_st)
                        else:
                            add_card_line([(style, f"   🎵 {c_str}")], hovered=hovered, is_checked=checked, border_style=border_st)

            if not is_table:
                bot_l = "┗" if checked else "╰"
                bot_r = "┛" if checked else "╯"
                hz = "━" if checked else "─"
                res.append((border_st, f" {bot_l}{hz * (inner_w + 2)}{bot_r}\n"))
            else:
                if i < len(options_dicts) - 1:
                    empty_prefix = " " * len(prefix)
                    add_line([("class:meta", empty_prefix + borders["mid"])], fill_bg=False)

        if not is_table:
            res.append(("", " \n" * 8))

        if res and res[-1][1].endswith("\n"):
            res[-1] = (res[-1][0], res[-1][1][:-1])

        return res

    def get_footer_text():
        try:
            columns = get_app().output.get_size().columns
        except Exception:
            columns, _ = shutil.get_terminal_size((80, 24))

        is_table, widths, headers, borders = _get_table_layout(columns, is_multi, item_category)
        res = []

        if is_table and options_dicts:
            table_prefix = " " * (5 if is_multi else 3)
            res.append(("class:meta", table_prefix + borders["bot"] + "\n"))

        res.append(("", "\n"))

        if options_dicts:
            res.append(("class:meta", f" Item {cursor_pos + 1} de {len(options_dicts)}\n"))

        if is_multi:
            res.append(("class:checkbox", f" * Selecionados: {len(selected_indices)}\n"))
            footer_msg = " [↑↓/jk] Mover   [Espaço] Selecionar   [t] Todos   [1-9] Ir para   [Enter] Confirmar"
        elif item_category == "artist":
            footer_msg = " [↑↓/jk] Mover   [1-9] Ir para   [Enter] Abrir artista"
        else:
            footer_msg = " [↑↓/jk] Mover   [1-9] Ir para   [Enter] Confirmar"

        if get_cwidth(footer_msg) > columns:
            trunc_msg = ""
            w = 0
            for char in footer_msg:
                cw = get_cwidth(char)
                if w + cw > columns - 1:
                    break
                trunc_msg += char
                w += cw
            res.append(("class:footer", trunc_msg + "\n"))
        else:
            res.append(("class:footer", footer_msg + "\n"))

        return res

    header_window = Window(content=FormattedTextControl(text=get_header_text), dont_extend_height=True)
    list_window = Window(
        content=FormattedTextControl(text=get_list_text, focusable=True),
        scroll_offsets=ScrollOffsets(top=2, bottom=8),
        wrap_lines=False,
    )
    footer_window = Window(content=FormattedTextControl(text=get_footer_text), dont_extend_height=True)

    layout = Layout(HSplit([header_window, list_window, footer_window]))
    app = Application(
        layout=layout, key_bindings=bindings, full_screen=True, style=pt_style, mouse_support=True,
    )

    res = await app.run_async()
    if isinstance(res, Exception):
        raise res
    return res


class TidalDL:
    def __init__(
        self,
        settings: TidalDLSettings,
        *,
        paths: Optional[dict] = None,
        http: Optional[HttpClient] = None,
    ):
        self.settings = settings
        self.paths = paths or get_config_paths()
        self.store = CredentialStore(
            self.paths["credentials_file"], use_keyring=not settings.disable_keyring
        )
        self._http = http
        self.api: Optional[TidalAPI] = None
        self.downloader: Optional[Downloader] = None

    @property
    def downloads_db(self) -> Optional[str]:
        return None if self.settings.no_database else self.paths["tidal_db"]

    # -- ciclo de vida -----------------------------------------------------

    def _persist(self, creds: Credentials) -> None:
        try:
            self.store.save(creds)
        except OSError as exc:
            ui.warn(f"não foi possível salvar o token renovado: {exc}")

    async def initialize(self) -> "TidalDL":
        creds = self.store.load()
        if creds is None:
            raise AuthenticationError("Você não está logado. Rode: tidal-dl login")
        if self._http is None:
            self._http = create_client(
                requests_per_minute=self.settings.requests_per_minute, retries=self.settings.retries
            )
        self.api = TidalAPI(self._http, creds, on_refresh=self._persist)
        await self.api.ensure_token()
        self.downloader = Downloader(self.api, self.settings, db_path=self.downloads_db)
        return self

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    # -- downloads ---------------------------------------------------------

    def _need(self) -> tuple[TidalAPI, Downloader]:
        if self.api is None or self.downloader is None:
            raise TidalDLException("TidalDL não inicializado (chame initialize()).")
        return self.api, self.downloader

    async def download_from_id(self, item_id: Any, kind: str = "album", *, include_eps: bool = False) -> bool:
        """Baixa um item. Devolve True se tudo deu certo (ou já existia)."""
        api, dl = self._need()
        kind = kind.lower()
        try:
            if kind == "album":
                return (await dl.download_album(item_id)).ok
            if kind == "track":
                return (await dl.download_track(item_id)).ok
            if kind == "playlist":
                return (await dl.download_playlist(str(item_id))).ok
            if kind == "artist":
                return await self._download_artist(item_id, include_eps)
            if kind == "video":
                return (await dl.download_video(item_id)).ok
        except ResourceNotFoundError:
            ui.error(f"{kind} {item_id} não encontrado.")
            return False
        except NonStreamable as exc:
            ui.error(f"Indisponível: {exc}")
            return False
        raise TidalDLException(f"tipo de item desconhecido: {kind}")

    async def _download_artist(self, artist_id: Any, include_eps: bool) -> bool:
        api, dl = self._need()
        info = await api.get_artist(artist_id)
        albums = await api.get_artist_albums(artist_id)
        if include_eps:
            extra = await api.get_artist_albums(artist_id, eps_and_singles=True)
            seen = {a.id for a in albums}
            albums += [a for a in extra if a.id not in seen]
        albums.sort(key=lambda a: (a.release_date or "9999", a.title))
        ui.info(f"{info.get('name', artist_id)}: {len(albums)} álbum(ns).")
        ok = True
        for a in albums:
            ok = (await dl.download_album(a.id)).ok and ok
        return ok

    async def handle_url(self, url: str, *, include_eps: bool = False) -> bool:
        parsed = parse_url(url)
        if not parsed:
            ui.error(f"URL não reconhecida: {url}")
            return False
        kind, ident = parsed
        return await self.download_from_id(ident, kind, include_eps=include_eps)

    async def download_urls(self, urls: list[str], *, include_eps: bool = False) -> dict:
        ok = failed = 0
        for u in urls:
            try:
                good = await self.handle_url(u, include_eps=include_eps)
            except AuthenticationError:
                raise
            except TidalDLException as exc:
                ui.error(f"{u}: {exc}")
                good = False
            ok, failed = (ok + 1, failed) if good else (ok, failed + 1)
        return {"ok": ok, "failed": failed}

    # -- busca -------------------------------------------------------------

    async def search(self, kind: str, query: str, limit: int = 15) -> list[dict]:
        api, _ = self._need()
        page = await api.search(SEARCH_KINDS[kind], query, limit=limit)
        return page.items

    @staticmethod
    def describe(kind: str, item: dict) -> tuple[str, str]:
        """``(id, texto)`` de um resultado de busca."""
        if kind == "album":
            a = Album.from_dict(item)
            return str(a.id), f"{a.album_artist} - {a.full_title} ({a.year}) [{a.audio_quality or '?'}]"
        if kind == "track":
            art = (item.get("artist") or {}).get("name", "?")
            alb = (item.get("album") or {}).get("title", "")
            return str(item.get("id")), f"{art} - {item.get('title')} · {alb} ({human_duration(item.get('duration'))})"
        if kind == "artist":
            return str(item.get("id")), str(item.get("name", "?"))
        return str(item.get("uuid")), f"{item.get('title')} ({item.get('numberOfTracks', '?')} faixas)"

    async def lucky(self, query: str, kind: str = "album", number: int = 1) -> bool:
        items = await self.search(kind, query, limit=max(number, 1))
        if not items:
            ui.warn(f"Nada encontrado para: {query}")
            return False
        ok = True
        for it in items[:number]:
            ident, text = self.describe(kind, it)
            ui.info(f"Sorte: {text}")
            ok = await self.download_from_id(ident, kind) and ok
        return ok

    async def interactive(self, query: str, kind: str = "album", ask=None) -> bool:
        """Busca e abre a TUI de seleção em tela cheia (prompt_toolkit) --
        tabela alinhada em telas largas, cartões em telas estreitas, seleção
        múltipla com [espaço]/[t], atalhos 1-9/g/G, Ctrl+C/Esc cancela
        (mesmo estilo/atalhos do qobuz-dl-ultra).

        ANTES: menu por número + input() de texto (``parse_selection``),
        sem pré-visualização de metadados além de uma linha corrida por
        resultado.

        ``ask`` é aceito só por compatibilidade com chamadores antigos e é
        ignorado (a seleção agora é sempre pela TUI); testes devem mockar
        o ``_tui_select`` deste módulo.
        """
        items = await self.search(kind, query, limit=20)
        if not items:
            ui.warn(f"Nada encontrado para: {query}")
            return False
        options = [self._search_option(kind, it) for it in items]
        try:
            picks = await _tui_select(f"RESULTADOS: {query}", options, is_multi=True, item_category=kind)
        except KeyboardInterrupt:
            ui.info("Cancelado.")
            return False
        if not picks:
            ui.info("Nada selecionado.")
            return False
        ok = True
        for opt, _idx in picks:
            ok = await self.download_from_id(opt["id"], kind) and ok
        return ok

    @staticmethod
    def _search_option(kind: str, item: dict) -> dict:
        """``{"id": ..., "meta": {...}}`` pro _tui_select, a partir de UM
        resultado de busca cru da API. O formato de ``meta`` depende do
        ``item_category`` (ver interactive_ui._get_table_layout / o loop
        de desenho em ``_tui_select``)."""
        if kind == "album":
            a = Album.from_dict(item)
            ql = QUALITY_SHORT.get(a.audio_quality, a.audio_quality or "?")
            return {"id": str(a.id), "meta": {
                "title": a.full_title, "artist": a.album_artist, "type": a.release_type,
                "year": a.year, "tracks_count": a.number_of_tracks, "quality": ql,
            }}
        if kind == "track":
            t = Track.from_dict(item)
            ql = QUALITY_SHORT.get(t.audio_quality, t.audio_quality or "?")
            return {"id": str(t.id), "meta": {
                "title": t.full_title, "artist": t.artist_names, "album": t.album_title,
                "type": "Faixa", "duration": human_duration(t.duration), "quality": ql,
            }}
        if kind == "playlist":
            p = Playlist.from_dict(item)
            return {"id": p.uuid, "meta": {
                "name": p.title, "owner": p.creator or "Tidal", "count": p.number_of_tracks,
                "duration": human_duration(p.duration),
            }}
        ar = Artist.from_dict(item)
        return {"id": str(ar.id), "meta": {"name": ar.name, "count": ""}}

    # ==========================================================================
    # Modo interativo completo (estilo qobuz-dl-ultra): tela de TIPO -> tela de
    # TERMO (ou Favoritos) -> tabela de RESULTADOS, cada etapa separada, com
    # loop pra buscar de novo sem sair do programa. `interactive()` acima
    # continua existindo pra uso direto/scriptável (`search -t album termo`);
    # isso aqui é só pro `search`/`i`/`fun` chamado SEM argumentos.
    # ==========================================================================
    _TYPE_MENU = {"Álbuns": "album", "Faixas": "track", "Artistas": "artist", "Playlists": "playlist"}
    _FAVORITE_PAGE_METHOD = {
        "album": "favorite_albums_page", "track": "favorite_tracks_page",
        "artist": "favorite_artists_page", "playlist": "favorite_playlists_page",
    }

    async def interactive_menu(self) -> bool:
        """Tela de tipo (Álbuns/Faixas/Artistas/Playlists/Favoritos) -> tela de
        termo (ou lista de favoritos) -> tabela de resultados. Ctrl+C/Esc em
        qualquer tela volta pra anterior; na tela de tipo, sai do modo.
        """
        any_ok = False
        while True:
            try:
                choice, _ = await _tui_select(
                    "MODO INTERATIVO -- O que você quer baixar?",
                    ["Álbuns", "Faixas", "Artistas", "Playlists", "Favoritos"],
                    is_multi=False, item_category="filter",
                )
            except KeyboardInterrupt:
                return any_ok

            if choice == "Favoritos":
                ok = await self._interactive_favorites_menu()
            else:
                ok = await self._interactive_search_loop(self._TYPE_MENU[choice])
            any_ok = any_ok or ok

    async def _interactive_favorites_menu(self) -> bool:
        """Sub-tela: escolhe QUAL favorito (álbum/faixa/artista/playlist),
        busca a lista de uma vez (sem termo) e mostra a mesma tabela de
        resultados. Ctrl+C/Esc volta pro menu de tipo.
        """
        try:
            choice, _ = await _tui_select(
                "FAVORITOS -- Quais você quer explorar?",
                ["Álbuns", "Faixas", "Artistas", "Playlists"],
                is_multi=False, item_category="filter",
            )
        except KeyboardInterrupt:
            return False
        kind = self._TYPE_MENU[choice]
        method = getattr(self.api, self._FAVORITE_PAGE_METHOD[kind])
        try:
            page = await method(limit=100)
        except TidalDLException as exc:
            ui.error(f"Não deu pra buscar seus favoritos: {exc}")
            return False
        raw_items = [e.get("item") if isinstance(e.get("item"), dict) else e for e in (page.items or [])]
        return await self._select_and_download(f"MEUS FAVORITOS: {choice}", kind, raw_items)

    async def _interactive_search_loop(self, kind: str) -> bool:
        """Pede o termo (tela própria, com prompt_toolkit) em loop até
        Ctrl+C/Esc/linha vazia duas vezes seguidas -- cada busca bem-sucedida
        mostra a tabela de resultados na sequência.
        """
        session = PromptSession()
        rotulo = {"album": "álbuns", "track": "faixas", "artist": "artistas", "playlist": "playlists"}[kind]
        any_ok = False
        while True:
            ui.section(f"BUSCAR {rotulo.upper()}")
            prompt_message = FormattedText([
                ("class:prompt_text", " O que você está procurando? "),
                ("class:prompt_hint", "[Ctrl+C volta ao menu]\n"),
                ("class:prompt_cursor", " ❯ "),
            ])
            try:
                query = await session.prompt_async(prompt_message, style=prompt_style)
            except (EOFError, KeyboardInterrupt):
                return any_ok
            query = query.strip()
            if not query:
                continue
            items = await self.search(kind, query, limit=20)
            if not items:
                ui.warn(f"Nada encontrado para: {query}")
                continue
            ok = await self._select_and_download(f"RESULTADOS: {query}", kind, items)
            any_ok = any_ok or ok

    async def _select_and_download(self, title: str, kind: str, raw_items: list[dict]) -> bool:
        """Tabela de seleção (múltipla) a partir de uma lista já buscada
        (busca normal OU favoritos) + download do que for escolhido."""
        options = [self._search_option(kind, it) for it in raw_items]
        try:
            picks = await _tui_select(title, options, is_multi=True, item_category=kind)
        except KeyboardInterrupt:
            ui.info("Cancelado.")
            return False
        if not picks:
            ui.info("Nada selecionado.")
            return False
        ok = True
        for opt, _idx in picks:
            ok = await self.download_from_id(opt["id"], kind) and ok
        return ok




def parse_selection(text: str, maximum: int) -> list[int]:
    """``"1,3-5"`` -> ``[1, 3, 4, 5]`` (só valores em 1..maximum, sem repetir)."""
    if not text or text.lower() in ("q", "quit", "sair"):
        return []
    picked: list[int] = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                rng = range(min(a, b), max(a, b) + 1)
            else:
                rng = range(int(part), int(part) + 1)
        except ValueError:
            continue
        for n in rng:
            if 1 <= n <= maximum and n not in picked:
                picked.append(n)
    return picked


def read_url_file(path: str) -> list[str]:
    """Lê URLs de um .txt (uma por linha; ``#`` comenta)."""
    urls = []
    with open(os.path.expanduser(path), encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                urls.append(line)
    return urls
