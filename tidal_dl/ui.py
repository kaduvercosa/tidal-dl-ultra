# Camada única de apresentação no terminal do tidal-dl-ultra.
#
# Centraliza tudo que é escrito na tela em UM ponto de saída (emit()),
# protegido por UM lock (print_lock) e com UMA única fonte de verdade para
# largura/breakpoints do terminal. Isso evita que:
#   - print() cru, logger.info() e tqdm.write() disputem o stdout ao mesmo
#     tempo e cortem as barras de progresso do tqdm no meio;
#   - cada arquivo recalcule a largura do terminal com teto/fallback
#     diferentes, fazendo a mesma tela parecer "celular" num painel e "PC"
#     no outro.
#
# Uso básico:
#   from tidal_dl import ui
#   ui.ok("Album baixado")            -> [+] verde
#   ui.warn("Faixa indisponivel")     -> [!] amarelo
#   ui.error("Falha de rede")         -> [!] vermelho
#   ui.step("Buscando letras...")     -> [*] cor de destaque
#   ui.detail("caminho/do/arquivo")   -> linha secundária recuada
#   ui.header("ALBUM", [("Artista", "Daft Punk"), ("Faixas", "14")])
#   ui.section("ESTATISTICAS")
#   ui.kv("Total de downloads", "1234")
#
# No boot da CLI, para que o `logging` de todos os módulos também respeite
# as barras de progresso do tqdm:
#   ui.configure(quiet=args.quiet, verbose=args.verbose, color=not args.no_color)
#   ui.install_logging()

import logging
import os
import shutil
import sys
import textwrap
import threading

from tidal_dl.color import (
    ACCENT_DARK,
    BG,
    ERROR,
    HIGHLIGHT,
    MUTED,
    RESET,
    SUCCESS,
    WARNING,
)

# --------------------------------------------------------------------------
# Lock único de escrita no terminal
# --------------------------------------------------------------------------
# downloader.py importa este mesmo objeto como `print_lock` -- existe um
# único lock para todo o processo, não um por módulo. Threads diferentes
# usando locks diferentes ainda embaralhariam a saída entre si.
print_lock = threading.Lock()

# --------------------------------------------------------------------------
# Larguras e breakpoints -- fonte única de verdade para todo o projeto
# --------------------------------------------------------------------------
MIN_WIDTH = 32  # piso: pipes/CI às vezes relatam 0 colunas
MAX_WIDTH = 100  # teto: linha longa demais cansa de ler em monitor grande
FALLBACK = (80, 24)

# Breakpoints responsivos unificados.
NARROW = 60  # celular / a-Shell no iPhone / Split View estreito
MEDIUM = 90  # iPad retrato, terminal de meia tela
# acima de MEDIUM -> "wide" (desktop, iPad paisagem)

LAYOUT_NARROW = "narrow"
LAYOUT_MEDIUM = "medium"
LAYOUT_WIDE = "wide"


def raw_width():
    # Largura real do terminal, sem teto nem piso aplicados.
    # Respeita a variável de ambiente COLUMNS quando definida (útil em CI
    # e para forçar uma largura específica em testes).
    """Return terminal width in characters without clamping."""
    env = os.environ.get("COLUMNS")
    if env:
        try:
            value = int(env)
            if value > 0:
                return value
        except ValueError:
            pass
    try:
        return shutil.get_terminal_size(fallback=FALLBACK).columns
    except Exception:
        return FALLBACK[0]


def width(max_width=MAX_WIDTH, min_width=MIN_WIDTH):
    # Largura utilizável para desenhar blocos de texto, já com teto e piso
    # aplicados. Função única que substitui os cálculos redundantes que
    # existiam espalhados por vários arquivos do projeto.
    """Return terminal width, clamped to 200 for ultra-wide displays."""
    return max(min(raw_width(), max_width), min_width)


def layout():
    # Classifica a largura atual do terminal em narrow/medium/wide, para
    # que outras funções decidam como se adaptar (empilhar valor, quebrar
    # linha, etc.).
    """Return the current layout mode ('narrow' or 'wide') based on terminal width."""
    cols = raw_width()
    if cols < NARROW:
        return LAYOUT_NARROW
    if cols < MEDIUM:
        return LAYOUT_MEDIUM
    return LAYOUT_WIDE


def is_narrow():
    # Atalho para checagens rápidas de "estou em tela estreita?".
    """Check if terminal is narrow (width < 80)."""
    return layout() == LAYOUT_NARROW


def progress_ncols():
    # Largura usada pelas barras de progresso do tqdm.
    # Com teto em MAX_WIDTH: sem isso, numa janela muito larga a barra de
    # progresso ocuparia a tela inteira.
    """Calculate optimal width for progress bars based on terminal size."""
    return max(min(raw_width() - 1, MAX_WIDTH), 20)


# --------------------------------------------------------------------------
# Detecção de capacidade do terminal (cor e unicode)
# --------------------------------------------------------------------------
# Cache module-level: a detecção só roda uma vez por processo, não a cada
# chamada de emit().
_color_enabled = None
_unicode_enabled = None
_quiet = False
_verbose = False


def _detect_color():
    # Decide se é seguro emitir sequências ANSI de cor.
    # Delega para color._detect_color_capability() em vez de reimplementar
    # a regra aqui -- assim as duas partes do projeto nunca divergem sobre
    # quando a cor deve estar ligada ou desligada (ex.: saída redirecionada
    # para um arquivo com `> log.txt`).
    """Detect if color output should be enabled based on environment and TTY."""
    from tidal_dl.color import _detect_color_capability

    return _detect_color_capability()


def _detect_unicode():
    # Assume unicode disponível se a codificação do stdout contiver "utf".
    """Detect if Unicode glyphs are supported by the terminal."""
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in enc


def color_enabled():
    # Getter com cache preguiçoso (lazy) para o resultado de _detect_color().
    """Check if color output is currently enabled."""
    global _color_enabled
    if _color_enabled is None:
        _color_enabled = _detect_color()
    return _color_enabled


def unicode_enabled():
    # Getter com cache preguiçoso para o resultado de _detect_unicode().
    """Check if Unicode output is currently enabled."""
    global _unicode_enabled
    if _unicode_enabled is None:
        _unicode_enabled = _detect_unicode()
    return _unicode_enabled


def is_quiet():
    """True quando --quiet está ativo (barras de progresso também ficam mudas)."""
    return _quiet


def configure(quiet=None, verbose=None, color=None, unicode=None):
    # Ponto único de ajuste do comportamento global da UI. Chamado uma vez,
    # no boot da CLI, com os valores vindos dos argumentos de linha de
    # comando. Parâmetros None são ignorados (mantêm o valor atual).
    """Configure UI system (color, Unicode, quiet/verbose modes)."""
    global _quiet, _verbose, _color_enabled, _unicode_enabled
    if quiet is not None:
        _quiet = bool(quiet)
    if verbose is not None:
        _verbose = bool(verbose)
    if color is not None:
        _color_enabled = bool(color)
    if unicode is not None:
        _unicode_enabled = bool(unicode)


def c(code):
    # Devolve o código ANSI passado, ou string vazia quando a cor está
    # desligada. Usado em toda função de emit para envolver texto com cor
    # sem precisar de um `if color_enabled()` em cada chamada.
    """Apply color escape sequence if color is enabled, otherwise return empty string."""
    return code if color_enabled() else ""


def _glyph(fancy, plain):
    # Escolhe entre o glifo unicode "bonito" e o equivalente ASCII simples,
    # dependendo do suporte detectado do terminal.
    """Return a Unicode glyph if supported, otherwise return ASCII fallback."""
    return fancy if unicode_enabled() else plain


def heavy_bar_char():
    # Caractere usado nas barras/separadores grossos (títulos, banners).
    """Return the heavy bar character for progress bars (Unicode-aware)."""
    return _glyph("\u2501", "=")  # ━


def light_bar_char():
    # Caractere usado em separadores finos.
    """Return the light bar character for progress bars (Unicode-aware)."""
    return _glyph("\u2500", "-")  # ─


def block_char():
    # Caractere usado para preencher barras de progresso proporcionais
    # (ex.: bar_gauge no ranking de artistas).
    """Return the block character for bar gauges (Unicode-aware)."""
    return _glyph("\u2588", "#")  # █


# --------------------------------------------------------------------------
# Saída: um único caminho para o terminal
# --------------------------------------------------------------------------
def emit(text="", end="\n"):
    # ÚNICO ponto de escrita no terminal usado pelo restante do módulo (e,
    # por extensão, por todo o programa via as funções ok/warn/error/etc.).
    # Usa tqdm.write() quando disponível, para que barras de progresso
    # ativas sejam apagadas e redesenhadas ao redor da mensagem em vez de
    # serem cortadas ao meio. Respeita --quiet (não imprime nada).
    """Print a message unless quiet mode is active."""
    if _quiet:
        return
    with print_lock:
        _write_locked(text, end)


def _write_locked(text, end):
    # Implementação real da escrita, já dentro do print_lock.
    # Tenta tqdm.write primeiro; se tqdm não estiver disponível, cai para
    # print() comum; se o terminal não suportar os caracteres unicode do
    # texto, reescreve substituindo o que não encaixa em vez de derrubar o
    # processo com UnicodeEncodeError.
    """Thread-safe write to stdout with optional flushing."""
    try:
        from tqdm import tqdm

        tqdm.write(str(text), end=end)
    except Exception:
        try:
            print(text, end=end, flush=True)
        except UnicodeEncodeError:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(
                str(text).encode(enc, errors="replace").decode(enc),
                end=end,
                flush=True,
            )


def emit_always(text="", end="\n"):
    # Igual a emit(), mas ignora --quiet. Reservado para mensagens que o
    # usuário precisa ver mesmo em modo silencioso: erros fatais e prompts
    # de confirmação.
    """Print a message regardless of quiet mode (for critical output)."""
    with print_lock:
        _write_locked(text, end)


# --------------------------------------------------------------------------
# Mensagens semânticas (linhas com tag colorida: [+], [!], [*], [-])
# --------------------------------------------------------------------------
# Tamanho de "[x] " -- o recuo das linhas de continuação alinha o texto sob
# o texto da primeira linha, não sob a tag.
_LARGURA_TAG = 4


def _tagged(color, tag, message):
    # Monta uma mensagem com tag colorida, já quebrada na largura do
    # terminal, e devolve a lista de linhas prontas para emit().
    # A quebra é necessária porque mensagens longas (ex.: avisos com
    # instruções de instalação) estourariam a largura do terminal sem isso.
    # A linha inteira (tag + texto) fica na cor semantica, nao so' a tag --
    # antes so' o "[!]"/"[+]" saia colorido e o resto ficava na cor padrao
    # do terminal, o que era uma regressao visual em relacao aos prints
    # antigos (ex.: `f"{YELLOW}[!] mensagem inteira{OFF}"`) que a migracao
    # pra ui.warn()/ui.error()/etc. tinha introduzido sem querer.
    # RESET é emitido em CADA linha de propósito, já que emit() escreve uma
    # linha por chamada e sem fechar a cor em cada uma ela vazaria para o
    # texto seguinte.
    """Wrap text with a color tag and optional icon."""
    limite = max(width() - _LARGURA_TAG, 12)
    partes = _wrap_lines(message, limite)
    pad = " " * _LARGURA_TAG

    linhas = [f"{c(color)}[{tag}] {partes[0]}{c(RESET)}"]
    linhas += [f"{c(color)}{pad}{parte}{c(RESET)}" for parte in partes[1:]]
    return linhas


def _emit_tagged(color, tag, message, sempre=False):
    # Emite cada linha produzida por _tagged() separadamente, escolhendo
    # entre emit() e emit_always() conforme o parâmetro `sempre`.
    """Emit a tagged message with consistent formatting."""
    escrever = emit_always if sempre else emit
    for linha in _tagged(color, tag, message):
        escrever(linha)


def ok(message):
    # Mensagem de sucesso -- prefixo "[+]" verde.
    """Print a success message (green checkmark)."""
    _emit_tagged(SUCCESS, "+", message)


def step(message, plain=False):
    # Mensagem de etapa em andamento -- prefixo "[*]" na cor de destaque.
    # plain=True usa a cor padrão do terminal (sem destaque), pra mensagens
    # que não devem competir visualmente com as etapas coloridas (ex.: busca
    # de letras, que já tem seu próprio resultado colorido em ok()/warn()).
    """Print a progress step message (arrow icon)."""
    _emit_tagged(RESET if plain else HIGHLIGHT, "*", message)


def info(message):
    # Informação neutra, sem prefixo colorido nem quebra automática de tag.
    """Print an informational message (info icon)."""
    emit(message)


def warn(message):
    # Mensagem de aviso -- prefixo "[!]" amarelo. Ignora --quiet (usa
    # emit_always): a própria ajuda da flag promete "mostra apenas avisos
    # e erros", então um aviso suprimido por --quiet contradiz a promessa
    # da flag. BUGFIX: antes chamava emit() puro e um --quiet escondia
    # avisos que o usuário tinha sido informado que continuariam visíveis.
    """Print a warning message (yellow warning icon)."""
    _emit_tagged(WARNING, "!", message, sempre=True)


def error(message):
    # Mensagem de erro -- prefixo "[!]" vermelho. Ignora --quiet (usa
    # emit_always) porque erro é informação que o usuário sempre precisa ver.
    """Print an error message (red X icon)."""
    _emit_tagged(ERROR, "!", message, sempre=True)


def skip(message):
    # Item ignorado/pulado -- prefixo "[-]" na cor apagada.
    """Print a skip/omit message (yellow circle icon)."""
    _emit_tagged(MUTED, "-", message)


def detail(message, indent=4):
    # Linha secundária: comunica hierarquia pelo RECUO, não pela cor (texto
    # sai na cor padrão do terminal). Quebra automaticamente na largura
    # disponível, respeitando o recuo.
    """Print a detail message with indentation (dim arrow)."""
    message = str(message)
    pad = " " * indent
    if len(message) + indent <= width():
        emit(f"{c(RESET)}{pad}{message}")
        return
    for line in _wrap_lines(message, width() - indent):
        emit(f"{c(RESET)}{pad}{line}")


def debug(message):
    # Só aparece quando --verbose está ativo.
    """Print a debug message if verbose mode is active."""
    if _verbose:
        emit(f"{c(MUTED)}[debug] {message}{c(RESET)}")


def blank():
    # Atalho para uma linha em branco.
    """Print a blank line unless quiet mode is active."""
    emit("")


# --------------------------------------------------------------------------
# Estrutura visual (regras, banners, seções, tabelas chave/valor)
# --------------------------------------------------------------------------
def rule(char=None, cols=None, color=HIGHLIGHT):
    # Linha divisória de largura total (usa a largura do terminal por
    # padrão, ou `cols` se especificado).
    """Print a horizontal rule line."""
    ch = char or heavy_bar_char()
    n = cols if cols is not None else width()
    emit(f"{c(color)}{ch * n}{c(RESET)}")


def banner(title, cols=None):
    # Título centralizado entre duas linhas grossas -- usado para
    # destacar seções importantes (relatório de stats, menu de playlist).
    """Print a banner with text centered in a box."""
    n = cols if cols is not None else width()
    bar = heavy_bar_char() * n
    emit(f"\n{c(HIGHLIGHT)}{bar}{c(RESET)}")
    emit(f"{c(BG)}{c(HIGHLIGHT)}{title.center(n)}{c(RESET)}")
    emit(f"{c(HIGHLIGHT)}{bar}{c(RESET)}\n")


def section(title):
    # Subtítulo de bloco dentro de um relatório maior.
    """Print a section header."""
    emit(f"  {c(BG)}{title}{c(RESET)}")


def kv(label, value, label_width=30, narrow_stack=False):
    # Par rótulo/valor, sempre dentro da largura do terminal. Escolhe entre
    # três modos conforme o espaço disponível:
    #   1) coluna alinhada (rótulo + valor lado a lado) -- padrão em telas
    #      médias/largas;
    #   2) rótulo e valor empilhados, com o valor quebrado em várias linhas,
    #      quando o valor não cabe na coluna (ex.: listas longas);
    #   3) "rotulo: valor" corrido em telas estreitas.
    """Print a key-value pair with aligned formatting."""
    value = str(value)
    total = width()

    if is_narrow():
        if narrow_stack or len(value) > total - 4 - len(label):
            emit(f"  {c(HIGHLIGHT)}{label}:{c(RESET)}")
            wrapped(value, indent=4)
        else:
            emit(f"  {c(HIGHLIGHT)}{label}:{c(RESET)} {value}")
        return

    # Espaço que sobra para o valor depois de "  " + rótulo + "  ".
    room = total - 2 - label_width - 2
    if len(value) <= room:
        emit(f"  {c(HIGHLIGHT)}{label:<{label_width}}{c(RESET)}  {value}")
    else:
        emit(f"  {c(HIGHLIGHT)}{label}{c(RESET)}")
        wrapped(value, indent=4)


def header(kind, rows):
    # Cabeçalho de operação (álbum, faixa, playlist, lote de urls): uma
    # barra grossa, o tipo entre colchetes, e uma lista de pares
    # rótulo/valor alinhados. A largura da barra acompanha o conteúdo real
    # (piso 20, teto = largura utilizável), então em telas estreitas ela
    # não vaza para a linha seguinte.
    """Print a header box with title and key-value pairs."""
    rows = list(rows)
    label_width = max((len(label) for label, _ in rows), default=8)

    header_line = f" [{kind}]"
    row_lines = [f" {label.upper():<{label_width}}  {value}" for label, value in rows]
    content_width = max([len(header_line)] + [len(r) for r in row_lines], default=20)
    bar_width = max(20, min(content_width, width()))
    bar = heavy_bar_char() * bar_width

    lines = [
        f"\n{c(HIGHLIGHT)}{bar}{c(RESET)}",
        f"{c(BG)} [{kind}]{c(RESET)}",
        "",
    ]
    for label, value in rows:
        lines.append(
            f" {c(HIGHLIGHT)}{label.upper():<{label_width}}{c(RESET)}  {value}"
        )
    lines.append(f"{c(HIGHLIGHT)}{bar}{c(RESET)}\n")
    emit("\n".join(lines))


def bar_gauge(value, peak, max_blocks=None):
    # Barrinha horizontal proporcional (ex.: ranking de artistas por
    # quantidade de faixas). `max_blocks` é derivado da largura real do
    # terminal quando não informado, em vez de um número fixo -- isso evita
    # que a barra estoure a linha em terminais estreitos.
    """Print a horizontal bar gauge visualization."""
    if max_blocks is None:
        max_blocks = max(6, min(24, width() - 56))
    peak = peak or 1
    length = max(1, round(value * max_blocks / peak)) if value else 0
    return f"{c(HIGHLIGHT)}{block_char() * length}{c(RESET)}"


def _wrap_lines(text, limit):
    # Wrapper fino sobre textwrap.wrap: garante um piso mínimo de largura
    # e sempre devolve pelo menos uma linha (mesmo que vazia).
    """Wrap long text to fit terminal width."""
    return textwrap.wrap(str(text), width=max(int(limit), 12)) or [""]


def wrapped(text, indent=0, cols=None):
    # Texto corrido quebrado na largura do terminal, com recuo opcional.
    # Emite RESET no início de CADA linha de propósito: garante que texto
    # explicativo sempre saia na cor padrão do terminal, sem herdar cor de
    # quem escreveu antes.
    """Print wrapped text with optional indentation."""
    n = (cols if cols is not None else width()) - indent
    pad = " " * indent
    for line in _wrap_lines(text, n):
        emit(f"{c(RESET)}{pad}{line}")


def truncate(text, limit):
    # Encurta uma string preservando o início, adicionando reticências
    # quando o limite é ultrapassado.
    """Truncate text to fit terminal width with ellipsis."""
    text = str(text)
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


# --------------------------------------------------------------------------
# Ponte com o módulo logging
# --------------------------------------------------------------------------
class TqdmLoggingHandler(logging.Handler):
    # Handler de logging que redireciona toda mensagem (logger.info,
    # logger.warning, etc. de qualquer módulo do projeto) para emit()/
    # emit_always() deste arquivo -- ou seja, passa a usar o mesmo lock e o
    # mesmo tqdm.write que as barras de progresso do downloader, em vez de
    # escrever direto no stdout por cima delas.

    def __init__(self, bypass_quiet=False):
        """Initialize logging handler that respects quiet mode unless bypass_quiet is True."""
        super().__init__()
        # bypass_quiet=True quando --log-level foi passado explicitamente:
        # nesse caso o filtro de nivel do proprio `logging` (setLevel) ja'
        # decide o que chega aqui, entao aplicar o gate de --quiet DE NOVO
        # em cima seria a pessoa pedir --log-level DEBUG e continuar sem
        # ver debug nenhum. Sem --log-level explicito, mantem o gate: e'
        # o comportamento padrao (quiet/verbose) descrito em --help.
        self.bypass_quiet = bypass_quiet

    def emit(self, record):
        """Print a message unless quiet mode is active."""
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        # BUGFIX: antes so' logging.ERROR+ escapava de --quiet. Mas --quiet
        # promete no --help "mostra apenas avisos e erros" -- um
        # logger.warning(...) real tambem precisa sobreviver, do contrario
        # a promessa da flag vale pra ui.warn() mas nao pra logging.warning(),
        # que e' o mesmo tipo de mensagem vindo de outro caminho de codigo.
        if record.levelno >= logging.WARNING or self.bypass_quiet:
            emit_always(msg)
        else:
            emit(msg)


def install_logging(level=None):
    # Substitui os handlers do logger raiz pelo TqdmLoggingHandler. Deve
    # ser chamado uma vez, no início da CLI, depois de configure().
    """Configure Python logging to route through the UI system."""
    explicit_level = level is not None
    if level is None:
        if _quiet:
            level = logging.WARNING
        elif _verbose:
            level = logging.DEBUG
        else:
            level = logging.INFO

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = TqdmLoggingHandler(bypass_quiet=explicit_level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    root.setLevel(level)

    # Bibliotecas de rede são ruidosas em DEBUG; mantém elas um nível acima
    # para não poluir a saída em --verbose.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "PIL"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))

    return handler


# Define explicitamente o que é exportado por `from tidal_dl.ui import *`.
__all__ = [
    "ACCENT_DARK",
    "LAYOUT_MEDIUM",
    "LAYOUT_NARROW",
    "LAYOUT_WIDE",
    "MAX_WIDTH",
    "MEDIUM",
    "MIN_WIDTH",
    "NARROW",
    "TqdmLoggingHandler",
    "banner",
    "bar_gauge",
    "blank",
    "block_char",
    "c",
    "color_enabled",
    "configure",
    "debug",
    "detail",
    "emit",
    "emit_always",
    "error",
    "header",
    "heavy_bar_char",
    "info",
    "install_logging",
    "is_narrow",
    "kv",
    "layout",
    "light_bar_char",
    "ok",
    "print_lock",
    "progress_ncols",
    "raw_width",
    "rule",
    "section",
    "skip",
    "step",
    "truncate",
    "unicode_enabled",
    "warn",
    "width",
    "wrapped",
]
