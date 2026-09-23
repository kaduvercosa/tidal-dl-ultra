"""Testes de settings (config.ini) e banco de dedup."""

import os
from types import SimpleNamespace

import pytest

from tidal_dl import db
from tidal_dl.exceptions import ConfigError
from tidal_dl.settings import TidalDLSettings


def test_defaults_validos():
    st = TidalDLSettings()
    st.validate()
    assert st.quality == 4 and st.allow_quality_fallback and st.write_sentinel


def test_roundtrip_config(tmp_path):
    f = str(tmp_path / "c.ini")
    st = TidalDLSettings(quality=2, lyrics=False, max_workers=3, delay=1.5, folder_format="{album_title}")
    st.save(f)
    back = TidalDLSettings.from_config(f)
    assert (back.quality, back.lyrics, back.max_workers, back.delay, back.folder_format) == (2, False, 3, 1.5, "{album_title}")
    if os.name == "posix":
        assert oct(os.stat(f).st_mode & 0o777) == "0o600"


def test_config_ausente_usa_defaults(tmp_path):
    assert TidalDLSettings.from_config(str(tmp_path / "nao.ini")).quality == 4


@pytest.mark.parametrize("bad", [
    {"quality": 9}, {"remux": "xyz"}, {"folder_format": "{nao_existe}"}, {"track_format": "{album_title}"},
])
def test_validate_rejeita(bad):
    with pytest.raises(ConfigError):
        TidalDLSettings(**bad).validate()


def test_config_valor_invalido_e_arquivo_ruim(tmp_path):
    f = tmp_path / "c.ini"
    f.write_text("[tidal]\nquality = abc\n")
    with pytest.raises(ConfigError):
        TidalDLSettings.from_config(str(f))
    f.write_text("sem secao valida\n")
    with pytest.raises(ConfigError):
        TidalDLSettings.from_config(str(f))


def test_limites_de_concorrencia_e_retries():
    st = TidalDLSettings(max_workers=99, retries=0)
    st.validate()
    assert st.max_workers == 16 and st.retries == 1


def test_apply_args_so_sobrepoe_o_que_veio(tmp_path):
    st = TidalDLSettings(quality=3, max_workers=4)
    args = SimpleNamespace(directory=str(tmp_path), quality=0, no_db=True, no_lyrics=True, no_fallback=True,
                           no_cover=True, no_sentinel=True)
    st.apply_args(args)
    assert st.quality == 0 and st.max_workers == 4 and st.no_database and not st.lyrics
    assert not st.allow_quality_fallback and not st.embed_art and not st.write_sentinel
    assert st.directory == str(tmp_path)


def test_apply_args_diretorio_relativo_no_ashell(monkeypatch, tmp_path):
    monkeypatch.setenv("TIDAL_DL_IOS_HOME", str(tmp_path))
    st = TidalDLSettings().apply_args(SimpleNamespace(directory="Musica"))
    assert st.directory == os.path.join(str(tmp_path), "Musica")


def test_db_dedup_por_qualidade_e_caminho(tmp_path):
    p = str(tmp_path / "t.db")
    pasta = tmp_path / "album"
    pasta.mkdir()
    assert db.is_downloaded(p, 1, "album") is None  # banco ainda não existe
    db.mark_downloaded(p, 1, "album", quality=2, saved_path=str(pasta), artist="A", album="B", release_date="2020-01-01")
    assert db.is_downloaded(p, 1, "album", 2) == str(pasta)
    assert db.is_downloaded(p, 1, "album", 1) == str(pasta)
    assert db.is_downloaded(p, 1, "album", 4) is None  # pediu qualidade maior: baixa de novo
    pasta.rmdir()
    assert db.is_downloaded(p, 1, "album", 2) is None  # pasta sumiu
    assert db.get_record(p, 1, "album") is None  # registro obsoleto foi descartado


def test_db_stats_e_purge(tmp_path):
    p = str(tmp_path / "t.db")
    db.mark_downloaded(p, 1, "album", quality=2, saved_path="", artist="A", album="B", release_date="2020-01-01")
    db.mark_downloaded(p, 2, "track", quality=2, file_format="FLAC", artist="A", title="T")
    st = db.get_stats(p)
    assert st["total"] == 2 and st["by_type"] == {"album": 1, "track": 1} and st["by_format"] == {"FLAC": 1}
    assert st["by_year"] == {"2020": 1} and len(st["latest"]) == 2
    assert db.purge(p) == 2 and db.get_stats(p)["total"] == 0
    assert db.get_stats(str(tmp_path / "nao.db"))["total"] == 0
