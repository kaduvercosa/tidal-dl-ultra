"""Testes do remux Python puro FLAC-em-fMP4 -> FLAC."""

import os

import pytest

from fakes import box, make_init, make_segment
from tidal_dl import fmp4
from tidal_dl.exceptions import DownloadError


def _write(tmp_path, data, name="a.mp4"):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_remux_devolve_streaminfo_e_frames_em_ordem(tmp_path):
    frames = [os.urandom(900), os.urandom(500), os.urandom(50)]
    data = make_init(96000, 2, 24) + b"".join(make_segment(f) for f in frames)
    src, dst = _write(tmp_path, data), str(tmp_path / "a.flac")
    info = fmp4.remux_fmp4_flac(src, dst)
    assert (info["sample_rate"], info["bit_depth"], info["channels"]) == (96000, 24, 2)
    out = open(dst, "rb").read()
    assert out[:4] == b"fLaC" and out.endswith(b"".join(frames))
    assert out[4] & 0x80  # único bloco = último


def test_44k_16bit(tmp_path):
    src, dst = _write(tmp_path, make_init(44100, 2, 16) + make_segment(b"x" * 10)), str(tmp_path / "o.flac")
    info = fmp4.remux_fmp4_flac(src, dst)
    assert (info["sample_rate"], info["bit_depth"]) == (44100, 16)


def test_box_de_64_bits(tmp_path):
    payload = b"AUDIO" * 20
    mdat64 = (1).to_bytes(4, "big") + b"mdat" + (16 + len(payload)).to_bytes(8, "big") + payload
    src = _write(tmp_path, make_init() + box(b"moof", b"\0" * 8) + mdat64)
    dst = str(tmp_path / "o.flac")
    fmp4.remux_fmp4_flac(src, dst)
    assert open(dst, "rb").read().endswith(payload)


def test_sem_dfla_falha_e_remove_saida(tmp_path):
    moov = box(b"moov", box(b"trak", b"\0" * 20))
    src, dst = _write(tmp_path, moov + make_segment(b"x")), str(tmp_path / "o.flac")
    with pytest.raises(DownloadError):
        fmp4.remux_fmp4_flac(src, dst)
    assert not os.path.exists(dst)


def test_sem_audio_e_truncado(tmp_path):
    dst = str(tmp_path / "o.flac")
    with pytest.raises(DownloadError):
        fmp4.remux_fmp4_flac(_write(tmp_path, make_init()), dst)
    trunc = make_init() + box(b"moof", b"\0" * 8) + (100).to_bytes(4, "big") + b"mdat" + b"curto"
    with pytest.raises(DownloadError):
        fmp4.remux_fmp4_flac(_write(tmp_path, trunc, "t.mp4"), dst)
    assert not os.path.exists(dst)


def test_mdat_antes_do_init(tmp_path):
    with pytest.raises(DownloadError):
        fmp4.remux_fmp4_flac(_write(tmp_path, make_segment(b"x") + make_init()), str(tmp_path / "o.flac"))


def test_flag_ultimo_bloco_so_no_final():
    si = bytes(34)
    padding = bytes(10)
    blocks = bytes([0x00]) + (34).to_bytes(3, "big") + si + bytes([0x80 | 1]) + (10).to_bytes(3, "big") + padding
    hdr = fmp4.build_flac_header(blocks)
    assert hdr[4] == 0x00 and hdr[4 + 4 + 34] == 0x81
