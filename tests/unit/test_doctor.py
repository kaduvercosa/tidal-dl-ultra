"""Testes do `tidal-dl doctor` (somente leitura, sem vazar segredos)."""

import json
import os
import sqlite3
import time

from tidal_dl import doctor
from tidal_dl.library_db import LibraryDB


def test_config_ausente_e_aviso_nao_falha(tmp_path):
    assert doctor.check_config(str(tmp_path / "nao.ini"))[0].level == doctor.WARN


def test_config_valido_e_invalido(tmp_path):
    cfg = tmp_path / "c.ini"
    cfg.write_text("[tidal]\nquality = 2\n")
    assert not any(r.level == doctor.FAIL for r in doctor.check_config(str(cfg)))
    cfg.write_text("[tidal]\nquality = 9\n")
    assert any(r.level == doctor.FAIL for r in doctor.check_config(str(cfg)))


def _creds(tmp_path, **kw):
    d = {"access_token": "SEGREDO123", "refresh_token": "REFRESH456", "token_expiry": time.time() + 7200,
         "auth_method": "pkce"}
    d.update(kw)
    p = tmp_path / "credentials.json"
    p.write_text(json.dumps(d))
    if os.name == "posix":
        os.chmod(p, 0o600)
    return str(p)


def test_credenciais_nao_vazam_token(tmp_path):
    res = doctor.check_credentials(_creds(tmp_path))
    text = " ".join(f"{r.name} {r.detail}" for r in res)
    assert "SEGREDO123" not in text and "REFRESH456" not in text
    assert res[-1].level == doctor.PASS and "pkce" in res[-1].detail


def test_credenciais_estados(tmp_path):
    assert doctor.check_credentials(str(tmp_path / "nao.json"))[0].level == doctor.WARN
    assert doctor.check_credentials(_creds(tmp_path, token_expiry=1, refresh_token=""))[-1].level == doctor.FAIL
    assert doctor.check_credentials(_creds(tmp_path, token_expiry=1))[-1].level == doctor.PASS
    assert "AAC" in doctor.check_credentials(_creds(tmp_path, auth_method="device_code"))[-1].detail
    (tmp_path / "credentials.json").write_text("{quebrado")
    assert doctor.check_credentials(str(tmp_path / "credentials.json"))[0].level == doctor.FAIL


def test_credenciais_permissao_aberta(tmp_path):
    if os.name != "posix":
        return
    p = _creds(tmp_path)
    os.chmod(p, 0o644)
    assert any(r.name == "Permissão do token" for r in doctor.check_credentials(p))


def test_directory_temporarios(tmp_path):
    (tmp_path / "[IN PROGRESS] X").mkdir()
    (tmp_path / "~tmp_1.part").write_bytes(b"x")
    nomes = [r.name for r in doctor.check_directory(str(tmp_path))]
    assert "Temporários" in nomes and "Pastas [IN PROGRESS]" in nomes


def test_downloads_db(tmp_path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE downloads (id TEXT, media_type TEXT, saved_path TEXT)")
    conn.execute("INSERT INTO downloads VALUES ('1','album','/nao/existe')")
    conn.commit()
    conn.close()
    res = doctor.check_downloads_db(str(db))
    assert res[0].level == doctor.PASS and any(r.name == "Registros obsoletos" for r in res)
    db.write_bytes(b"isto nao e sqlite" * 50)
    assert doctor.check_downloads_db(str(db))[0].level == doctor.FAIL


def test_library_db_presos(tmp_path):
    lib = LibraryDB(tmp_path / "l.db")
    a = lib.upsert_album("tidal", "1", "T", "A")
    lib.update_status(a, "downloading")
    assert "Álbuns presos" in [r.name for r in doctor.check_library_db(lib.path)]


def test_ffmpeg_e_opcional_e_render(tmp_path):
    assert all(r.level != doctor.FAIL for r in doctor.check_binaries())
    assert doctor.render([doctor.Check(doctor.WARN, "x")]) == 0
    assert doctor.render([doctor.Check(doctor.FAIL, "x")]) == 1
    cfg = tmp_path / "c.ini"
    cfg.write_text("[tidal]\ndirectory=/musica\n")
    assert doctor.resolve_directory(str(cfg)) == "/musica" and doctor.resolve_directory(str(tmp_path / "n")) is None
