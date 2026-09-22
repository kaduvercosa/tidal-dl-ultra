"""Camada de similaridade de texto com degradacao para a biblioteca padrao.

MOTIVO DESTE MODULO
-------------------
O `rapidfuzz` e' rapido (C++/Cython) mas e' um pacote **compilado**. Isso o
torna impossivel de instalar em ambientes que so' aceitam pacotes Python
puros -- o caso do a-Shell no iOS/iPadOS, que declara na propria
documentacao que `pip install` so' funciona "if they are pure Python".

ANTES: `cli.py` fazia `from rapidfuzz import ...` no topo do arquivo, entao
o `rapidfuzz` era obrigatorio so' pra **importar** o CLI. Sem ele o programa
nao subia -- nem `tidal-dl --help` funcionava -- mesmo que a unica coisa que
ele fizesse fosse sugerir o nome certo de uma chave de config digitada
errado.

DEPOIS: o `rapidfuzz` e' usado quando esta presente (mesma velocidade de
antes, nada muda em quem ja o tem instalado) e cai pro `difflib` da
biblioteca padrao quando nao esta. O `difflib` e' mais lento, mas resolve o
mesmo problema, e "mais lento" e' infinitamente melhor que "nao roda".

Toda a decisao mora aqui, num lugar so', em vez de espalhar `try/except
ImportError` pelos modulos que precisam comparar strings.
"""

import difflib
from types import ModuleType
from typing import Optional

_rf_fuzz: Optional[ModuleType]
_rf_process: Optional[ModuleType]

try:
    from rapidfuzz import fuzz as _rf_fuzz
    from rapidfuzz import process as _rf_process

    RAPIDFUZZ_DISPONIVEL = True
except ImportError:  # pragma: no cover - depende do ambiente
    _rf_fuzz = None
    _rf_process = None
    RAPIDFUZZ_DISPONIVEL = False


def nome_do_motor() -> str:
    """Qual implementacao esta em uso. Util pra `--show-config`/diagnostico."""
    return "rapidfuzz" if RAPIDFUZZ_DISPONIVEL else "difflib (biblioteca padrao)"


def ratio(a: str, b: str) -> float:
    """Similaridade entre duas strings, **normalizada de 0.0 a 1.0**.

    Cuidado que ja causou bug neste projeto: `rapidfuzz.fuzz.ratio()` devolve
    0-100 e `difflib.SequenceMatcher.ratio()` devolve 0-1. Esta funcao
    padroniza tudo em 0-1 para que quem chama nao precise saber qual motor
    esta ativo.
    """
    if not a or not b:
        return 0.0
    if RAPIDFUZZ_DISPONIVEL:
        assert _rf_fuzz is not None
        return _rf_fuzz.ratio(a, b) / 100.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def melhor_match(consulta: str, opcoes, corte: float = 0.6):
    """Devolve a opcao mais parecida com `consulta`, ou None.

    Args:
        consulta: o texto digitado (possivelmente errado).
        opcoes: iteravel de candidatos validos.
        corte: similaridade minima de 0.0 a 1.0 para aceitar a sugestao.

    Returns:
        A string da melhor opcao, ou None se nada passar do corte.

    Devolve so' a string (nao a tupla `(match, score, indice)` do rapidfuzz)
    porque nenhum dos chamadores usa o score -- e assim os dois caminhos tem
    exatamente o mesmo contrato.
    """
    opcoes = list(opcoes)
    if not consulta or not opcoes:
        return None

    # Prende o corte na faixa valida. Sem isto os dois motores nao apenas
    # falham com um corte fora de faixa, eles falham de formas DIFERENTES:
    # o rapidfuzz levanta TypeError ("score_cutoff has to be in the range of
    # 0.0 - 100.0") e o difflib levanta ValueError ("cutoff must be in
    # [0.0, 1.0]"). Isso quebraria a promessa central deste modulo -- que os
    # dois caminhos se comportam igual -- justamente no caso de erro, onde
    # quem chama menos espera uma diferenca.
    corte = min(1.0, max(0.0, float(corte)))

    if RAPIDFUZZ_DISPONIVEL:
        assert _rf_fuzz is not None
        assert _rf_process is not None
        achado = _rf_process.extractOne(
            consulta, opcoes, scorer=_rf_fuzz.ratio, score_cutoff=corte * 100
        )
        return achado[0] if achado else None

    # `n=1` porque so' queremos a melhor; `cutoff` do difflib ja e' 0-1.
    achados = difflib.get_close_matches(consulta, opcoes, n=1, cutoff=corte)
    return achados[0] if achados else None
