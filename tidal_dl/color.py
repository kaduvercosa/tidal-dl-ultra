# ==============================================================================
# MÓDULO: color.py (TIDAL-DL-ULTRA)
# DESCRIÇÃO: Gerenciamento centralizado de cores ANSI, TrueColor (24-bit),
#            detecção de capacidades do terminal (NO_COLOR, FORCE_COLOR, TTY),
#            e prévias visuais para o assistente de temas (accent colors).
#
# ONDE PROCURAR quando precisar mexer em algo:
#   - "Cor não desliga com --no-color/NO_COLOR" -> _detect_color_capability()
#     e a variável COLOR_ON (é decidido 1x, na importação do módulo)
#   - Adicionar/editar cor fixa (RED, GREEN, ERROR, etc.) -> bloco logo após COLOR_ON
#   - Adicionar um novo preset de cor de destaque no wizard -> ACCENT_PRESETS
#   - "Cor de destaque não persiste entre execuções" -> _find_config_file(),
#     _load_accent_rgb() (lê accent_color do config.ini)
#   - Preview de cor mostrado no wizard (-r) -> accent_preview()
#   - NUNCA usar `colorama.init(...)` aqui de novo -- leia o bloco de BUGFIX
#     logo abaixo dos imports antes de mexer nisso.
# ==============================================================================

import configparser
import os
import shutil
import sys

from colorama import Fore, Style, just_fix_windows_console

# BUGFIX: aqui rodava `init(autoreset=True)` no import do modulo. Dois
# problemas serios:
#
# 1. `colorama.init()` SUBSTITUI `sys.stdout` por um wrapper que REMOVE as
#    sequencias ANSI sempre que a saida nao e' um TTY. Resultado: FORCE_COLOR=1
#    e `tidal-dl ... | less -R` nunca funcionavam, e a decisao sobre cor ficava
#    fora do controle do programa -- o oposto do que ui.py precisa para
#    implementar --no-color / NO_COLOR / FORCE_COLOR de forma coerente.
# 2. `autoreset=True` faz o colorama injetar um reset depois de CADA print.
#    Como o projeto ja' emite RESET explicito, isso duplicava resets e
#    atrapalhava as linhas de progresso multi-segmento do tqdm.
#
# `just_fix_windows_console()` faz APENAS o necessario: liga o suporte a
# VT/ANSI nos consoles legados do Windows, sem trocar o sys.stdout nem filtrar
# nada. Em Linux/macOS e' um no-op. Quem decide se a cor sai ou nao passa a ser
# `tidal_dl.ui.color_enabled()`.
just_fix_windows_console()


# --------------------------------------------------------------------------
# Porta unica de decisao sobre cor
# --------------------------------------------------------------------------
# BUGFIX: `ui.c()` era a unica porta que respeitava --no-color/NO_COLOR, mas
# as constantes deste modulo sao importadas por valor e usadas DIRETO em
# centenas de `print(f"{CYAN}...")` espalhados pelo projeto, que nunca
# passam por `ui.c()`. Resultado concreto: `NO_COLOR=1 tidal-dl` continuava
# despejando ANSI na tela inicial inteira, e `--no-color` vazava em outros
# modulos -- exatamente o contrario do que a ajuda da flag promete.
#
# A decisao agora acontece UMA vez, aqui no import, e as constantes ja'
# nascem vazias quando a cor esta desligada. Assim toda emissao do projeto
# fica correta de uma vez, sem precisar reescrever cada print. `ui.c()`
# continua valendo como segunda camada (para --no-color por argumento e para
# overrides em tempo de execucao); as duas juntas nao conflitam, porque
# concatenar string vazia e' inofensivo.
#
# Le `sys.argv` de proposito: o import deste modulo acontece antes de
# qualquer parsing de argumentos, entao esperar pelo argparse deixaria a
# tela inicial (impressa muito cedo) sem protecao.
def _detect_color_capability() -> bool:
    """Detect whether color output should be enabled based on environment and TTY."""
    if os.environ.get("NO_COLOR") is not None:  # https://no-color.org/
        return False
    if "--no-color" in sys.argv:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM", "").lower() in ("dumb", "") and os.name != "nt":
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


# Decisão global calculada 1x na importação do módulo (veja o comentário
# acima sobre por que não dá pra esperar o argparse rodar).
COLOR_ON = _detect_color_capability()


def _e(seq: str) -> str:
    """Devolve a sequencia ANSI, ou string vazia se a cor esta desligada."""
    return seq if COLOR_ON else ""


# --------------------------------------------------------------------------
# Cores/estilos fixos, já resolvidos via _e() (viram "" se COLOR_ON=False)
# --------------------------------------------------------------------------
# STYLE
DF = _e(Style.NORMAL)
BG = _e(Style.BRIGHT)
RESET = _e(Style.RESET_ALL)
# BUGFIX CRITICO: `OFF` era `Style.DIM` ([2m), mas as 239 ocorrencias de
# `{OFF}` no projeto usam ele como TERMINADOR -- `f"{GREEN}texto{OFF}"`.
# `[2m` nao encerra nada: ele ATIVA o modo esmaecido e DEIXA a cor
# anterior valendo. Consequencia dupla em cascata:
#   1. tudo depois de um `{OFF}` saia esmaecido (o "aspecto apagado");
#   2. a cor nunca era desligada, entao linhas seguintes SEM cor nenhuma
#      herdavam o accent -- por isso as descricoes da tela inicial apareciam
#      coloridas mesmo sendo emitidas como texto puro.
# Isso ficou escondido por anos porque o `colorama.init(autoreset=True)`
# antigo grudava um RESET no fim de CADA print, mascarando o erro. Ao trocar
# por `just_fix_windows_console()` (que nao mexe no stdout), o vazamento
# apareceu. `MUTED` continua existindo para quando esmaecer for a intencao.
OFF = _e(Style.RESET_ALL)

# Cores fixas (nao personalizaveis)
RED = _e(Fore.RED)
BLUE = _e(Fore.BLUE)
GREEN = _e(Fore.GREEN)
YELLOW = _e(Fore.YELLOW)
MAGENTA = _e(Fore.MAGENTA)

ERROR = _e(Fore.RED)
SUCCESS = _e(Fore.GREEN)
WARNING = _e(Fore.YELLOW)
WARNING_SAFE = _e(Fore.LIGHTRED_EX)
MUTED = _e(Style.DIM)

# --------------------------------------------------------------------------
# Cor de destaque (accent) -- personalizável pelo usuário via wizard
# --------------------------------------------------------------------------
# Cor de destaque padrao (azul aco) -- pode ser sobrescrita pelo usuario
# no wizard de configuracao (tidal-dl -r). O valor e' lido do config.ini
# na importacao do modulo, entao vale pra toda a sessao sem precisar
# passar o objeto de settings por todo o codigo.
_DEFAULT_ACCENT = _e("\033[38;2;95;168;211m")
_DEFAULT_ACCENT_RGB = (95, 168, 211)

# Paleta de cores predefinidas exposta pro wizard. Cada entrada:
#   (nome_exibicao, codigo_rgb_string, escape_ansi)
# O campo rgb_string e' o que vai gravado em config.ini: "R;G;B"
# Para adicionar um novo preset: só incluir uma nova tupla aqui, seguindo
# o mesmo formato; o wizard (_pick_accent_color em cli.py) lista tudo
# dinamicamente a partir desta lista.
ACCENT_PRESETS = [
    ("Azul Aço     (padrão)", "95;168;211", "\033[38;2;95;168;211m"),
    ("Roxo Lavanda", "180;140;255", "\033[38;2;180;140;255m"),
    ("Verde Menta", "100;220;160", "\033[38;2;100;220;160m"),
    ("Laranja Âmbar", "255;165;80", "\033[38;2;255;165;80m"),
    ("Rosa Pastel", "255;130;180", "\033[38;2;255;130;180m"),
    ("Teal Aqua", "64;196;192", "\033[38;2;64;196;192m"),
    ("Dourado", "220;180;60", "\033[38;2;220;180;60m"),
    ("Coral", "255;100;100", "\033[38;2;255;100;100m"),
    ("Personalizada (RGB)...", None, None),
]

# Com a cor desligada, o preview de cada preset nao tem o que mostrar: zera o
# escape (3o campo) preservando o `None` da opcao "Personalizada", que e' o
# que o wizard usa pra identificar aquele item.
if not COLOR_ON:
    ACCENT_PRESETS = [
        (name, rgb, "" if escape else escape) for name, rgb, escape in ACCENT_PRESETS
    ]


def _find_config_file():
    """Localiza o config.ini usando a mesma logica de utils.get_config_paths()
    mas sem importar utils (evita importacao circular no boot do modulo)."""
    ios_home = os.environ.get("TIDAL_DL_IOS_HOME")
    config_dir = os.environ.get("CONFIG_DIR")

    if not config_dir:
        if ios_home:
            config_dir = ios_home
        elif os.name == "nt":
            config_dir = os.environ.get("APPDATA", "")
        else:
            home_dir = os.environ.get("HOME", "")
            if "Containers/Data/Application" in home_dir:
                config_dir = os.path.join(home_dir, "Documents")
            else:
                config_dir = os.path.join(home_dir, ".config")

    return os.path.join(config_dir, "tidal-dl", "config.ini")


def _rgb_escape(r, g, b) -> str:
    """Generate 24-bit RGB ANSI escape sequence, respecting COLOR_ON setting."""
    # Passa pela mesma porta: e' daqui que saem ACCENT/HIGHLIGHT/INFO/PROGRESS,
    # as cores mais usadas do programa.
    return _e(f"\033[38;2;{r};{g};{b}m")


def _darken(rgb: tuple, factor: float = 0.55) -> tuple:
    """Escurece um RGB multiplicando cada canal por 'factor' (0-1). Usado
    pra derivar uma variante mais escura da cor de destaque escolhida no
    wizard (tidal-dl -r), sem precisar de uma segunda cor cadastrada a
    parte -- 1 escolha do usuario, 2 tons derivados automaticamente."""
    r, g, b = rgb
    return (
        max(0, min(255, int(r * factor))),
        max(0, min(255, int(g * factor))),
        max(0, min(255, int(b * factor))),
    )


def _load_accent_rgb() -> tuple:
    """Le accent_color do config.ini e retorna a tupla (r, g, b).
    Falha silenciosamente pro padrao se o arquivo nao existir ou o valor
    estiver invalido -- nunca deve crashar o boot do programa."""
    try:
        cfg_file = _find_config_file()
        if not os.path.exists(cfg_file):
            return _DEFAULT_ACCENT_RGB

        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(cfg_file, encoding="utf-8")
        rgb = cfg.get("tidal", "accent_color", fallback="").strip()

        if not rgb:
            return _DEFAULT_ACCENT_RGB

        parts = [int(x.strip()) for x in rgb.split(";")]
        if len(parts) == 3 and all(0 <= p <= 255 for p in parts):
            return tuple(parts)
    except (ValueError, configparser.Error, OSError):
        # Config corrompido/mal formatado (ex.: accent_color com lixo) --
        # cai pro padrão de propósito, sem barulho. Restrito a estes tipos
        # (em vez de Exception genérico) pra não mascarar um bug de verdade
        # aqui, e não vira log porque isto roda no import do módulo, antes
        # de qualquer logging estar configurado (ver comentário abaixo).
        pass

    return _DEFAULT_ACCENT_RGB


# Lida 1x no import: é o valor que vale durante toda a execução do programa
# (não é relido depois, então se o usuário editar o config.ini manualmente
# no meio de uma execução, o accent só muda na próxima vez que o programa rodar).
_ACCENT_RGB = _load_accent_rgb()
_ACCENT = _rgb_escape(*_ACCENT_RGB)


# Variante mais escura da mesma cor de destaque (usada em elementos
# secundários, ex. bordas, texto menos importante)
ACCENT_DARK = _rgb_escape(*_darken(_ACCENT_RGB))

# HIGHLIGHT/INFO/PROGRESS são todos apelidos da mesma cor de destaque (_ACCENT).
# Existem como nomes separados só por legibilidade semântica no resto do
# projeto (ex: usar INFO em mensagens informativas, PROGRESS em barras).
HIGHLIGHT = _ACCENT
INFO = _ACCENT
PROGRESS = _ACCENT


def accent_preview(escape: str, label: str = "") -> str:
    """Retorna uma string com preview do `escape` em fundo escuro E claro.
    Adapta automaticamente para o tamanho da tela (celular vs iPad/PC)."""
    cols, _ = shutil.get_terminal_size((80, 24))

    dark_bg = _e("\033[40m")  # Fundo Preto
    light_bg = _e("\033[107m")  # Fundo Branco Brilhante
    dark_fg = _e("\033[97m")  # Texto Branco Brilhante
    light_fg = _e("\033[30m")  # Texto Preto

    # Aumentado para 115 colunas para acionar o modo escadinha no tablet em pé
    if cols < 115:
        # Modo responsivo: Quebra 2 linhas (\n\n) pra sair da frente do nome da cor.
        # Adiciona margem exata e \n no final para separar da próxima opção da lista.
        dark = f"{dark_bg} {escape}[FX] {dark_fg}The Weeknd {RESET}"
        light = f"{light_bg} {escape}[FX] {light_fg}The Weeknd {RESET}"

        # BUGFIX (tela estreita): o recuo era fixo em 10 espacos, entao a
        # linha media 36 colunas visiveis (10 recuo + 9 do rotulo + 17 da
        # amostra) e estourava em qualquer terminal menor que isso -- caso
        # real do a-Shell no iPad em Split View. O recuo agora cede espaco
        # conforme a largura disponivel, com piso de 2 para nao colar na
        # borda. O bloco de amostra em si nao encolhe: ele E' o conteudo.
        indent = " " * max(2, min(10, cols - 26))

        return f"\n\n{indent}Escuro:  {dark}\n{indent}Claro:   {light}\n"
    else:
        # Modo largo (iPad na horizontal ou PC)
        dark = f"{dark_bg} {escape}[FAIXA] {dark_fg}ARTISTA {escape}The Weeknd {RESET}"
        light = (
            f"{light_bg} {escape}[FAIXA] {light_fg}ARTISTA {escape}The Weeknd {RESET}"
        )

        return f"  Escuro: {dark}    Claro: {light}"
