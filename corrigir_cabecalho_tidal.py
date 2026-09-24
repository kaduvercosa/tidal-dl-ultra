#!/usr/bin/env python3
"""Corrige o cabeçalho de qualidade do álbum no tidal-dl-ultra."""
from pathlib import Path
import shutil
import py_compile
import sys

target = Path("tidal_dl/downloader.py")
if not target.is_file():
    print("ERRO: execute este script na raiz do repositório tidal-dl-ultra.")
    sys.exit(1)

text = target.read_text(encoding="utf-8")
old = (
    '        # A qualidade sera identificada durante o download.\\n'
    '        # Nao sondar previamente: isso duplica chamadas ao playback.\\n'
    '        acquired_label = "Será determinada pelo download"\\n'
    '        target_label = acquired_label'
)
new = (
    '        # O alvo vem da qualidade efetiva definida pelo catálogo/configuração.\\n'
    '        # Não sondar previamente: isso duplica chamadas ao playback.\\n'
    '        target_label = QUALITY_LABELS.get(\\n'
    '            album_quality, QUALITY_MAP.get(album_quality, "Desconhecida")\\n'
    '        )\\n'
    '        # A qualidade adquirida é confirmada pelas faixas após o download.\\n'
    '        acquired_label = "Confirmada durante o download"'
)

if text.count(old) != 1:
    print("ERRO: bloco esperado não encontrado de forma única; arquivo não alterado.")
    sys.exit(2)

backup = target.with_suffix(".py.bak")
if not backup.exists():
    shutil.copy2(target, backup)

target.write_text(text.replace(old, new, 1), encoding="utf-8")
try:
    py_compile.compile(str(target), doraise=True)
except Exception as exc:
    shutil.copy2(backup, target)
    print(f"ERRO de sintaxe; original restaurado: {exc}")
    sys.exit(3)

print("OK: cabeçalho do álbum corrigido; sintaxe validada.")
print(f"Backup: {backup}")
