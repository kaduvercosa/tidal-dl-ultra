
from pathlib import Path
import py_compile
import subprocess
import sys

ROOT = Path.cwd()
FILE = ROOT / "tidal_dl" / "downloader.py"

old_album = '''        # SONDAGEM DE QUALIDADE ADQUIRIDA (PRE-FETCH)
        acquired_label = "Desconhecida"
        if tracks:
        target_label = acquired_label  # O usuário determinou que o alvo exibido espelha o playback
'''

new_album = '''        # SONDAGEM DE QUALIDADE ADQUIRIDA (PRE-FETCH)
        acquired_label = "Desconhecida"
        if tracks:
            acquired_label = await self._probe_playback_quality(
                tracks[0],
                effective_quality(album, self.settings.quality),
                is_dolby_atmos(
                    album.audio_quality,
                    album.audio_modes,
                    album.media_metadata_tags,
                ),
            )

        target_label = acquired_label
'''

old_playlist = '''        acquired_label = "Desconhecida"
        if tracks:

        ui.header("PLAYLIST", [
'''

new_playlist = '''        acquired_label = "Será determinada pelo download"
        target_label = acquired_label

        ui.header("PLAYLIST", [
'''

def main():
    if not FILE.exists():
        sys.exit(f"Arquivo não encontrado: {FILE}")

    text = FILE.read_text(encoding="utf-8")

    if text.count(old_album) != 1:
        sys.exit("ERRO: bloco de álbum não encontrado exatamente uma vez.")
    if text.count(old_playlist) != 1:
        sys.exit("ERRO: bloco de playlist não encontrado exatamente uma vez.")

    backup = FILE.with_suffix(".py.bak")
    backup.write_text(text, encoding="utf-8")
    print(f"Backup criado: {backup.name}")

    text = text.replace(old_album, new_album, 1)
    text = text.replace(old_playlist, new_playlist, 1)
    FILE.write_text(text, encoding="utf-8")
    print("Correções aplicadas.")

    try:
        py_compile.compile(str(FILE), doraise=True)
        print("OK: sintaxe Python válida.")
    except py_compile.PyCompileError as exc:
        print(f"ERRO de sintaxe: {exc}")
        FILE.write_text(backup.read_text(encoding="utf-8"),
                        encoding="utf-8")
        sys.exit("Arquivo restaurado pelo backup.")

    tests = [
        "tests/unit/test_cli.py",
        "tests/unit/test_core.py",
        "tests/unit/test_downloader.py",
        "tests/unit/test_video.py",
    ]

    cmd = [sys.executable, "-m", "pytest", *tests, "-q"]
    print("\nExecutando testes unitários...\n")

    result = subprocess.run(cmd, cwd=ROOT)
    print(f"\nPytest finalizado com código: {result.returncode}")

    if result.returncode != 0:
        print("Os testes falharam. O arquivo foi corrigido, "
              "mas precisa de investigação adicional.")

if __name__ == "__main__":
    main()
