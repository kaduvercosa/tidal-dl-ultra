
from pathlib import Path

file = Path("tidal_dl/downloader.py")

if not file.exists():
    print("ERRO: execute na raiz do repositorio.")
    raise SystemExit(1)

lines = file.read_text(encoding="utf-8").splitlines()

for start, end, label in [
    (795, 825, "BLOCO DO ALBUM"),
    (970, 995, "BLOCO DA PLAYLIST"),
]:
    print(f"\n{'=' * 15} {label} {'=' * 15}")
    for n in range(start, min(end, len(lines)) + 1):
        print(f"{n:04d}: {lines[n - 1]}")

print("\n========== OCORRENCIAS ==========")
for n, line in enumerate(lines, 1):
    if any(term in line for term in (
        "SONDAGEM DE QUALIDADE",
        "_probe_playback_quality(",
        "target_label =",
        "acquired_label =",
    )):
        print(f"{n:04d}: {line}")
