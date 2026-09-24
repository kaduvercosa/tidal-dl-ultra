
from pathlib import Path
import py_compile
import subprocess
import sys

ROOT = Path.cwd()
FILE = ROOT / "tidal_dl" / "downloader.py"

TESTS = [
    "tests/unit/test_cli.py",
    "tests/unit/test_core.py",
    "tests/unit/test_downloader.py",
    "tests/unit/test_video.py",
]

def main():
    if not FILE.exists():
        sys.exit("ERRO: downloader.py nao encontrado.")

    print("1. Validando sintaxe...")
    try:
        py_compile.compile(str(FILE), doraise=True)
        print("OK: sintaxe valida.\n")
    except py_compile.PyCompileError as exc:
        sys.exit(f"ERRO DE SINTAXE:\n{exc}")

    print("2. Executando testes unitarios...\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *TESTS, "-q"],
        cwd=ROOT,
    )

    print("\n" + "=" * 50)
    if result.returncode == 0:
        print("SUCESSO: todos os testes selecionados passaram.")
    else:
        print(f"FALHA: pytest retornou codigo {result.returncode}.")
        print("Envie a saida completa para investigacao.")
    print("=" * 50)

    sys.exit(result.returncode)

if __name__ == "__main__":
    main()
