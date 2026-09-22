"""Configuração do tidal-dl-ultra (config.ini) e settings em runtime.

Espelha o papel do settings.py do qobuz-dl-ultra: uma classe com defaults,
leitura do ``config.ini`` e sobreposição pelos argumentos de linha de comando
(argumento explícito > config.ini > default).
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field, fields
from typing import Any

from tidal_dl.constants import (
    DEFAULT_FOLDER,
    DEFAULT_MULTIPLE_DISC_TRACK,
    DEFAULT_QUALITY,
    DEFAULT_TRACK,
    FOLDER_PLACEHOLDERS,
    QUALITY_MAP,
    TRACK_PLACEHOLDERS,
)
from tidal_dl.exceptions import ConfigError
from tidal_dl.utils import atomic_write_text, default_download_folder, is_ios, validate_template

SECTION = "tidal"


@dataclass
class TidalDLSettings:
    directory: str = field(default_factory=default_download_folder)
    video_directory: str = "TidalVideos"
    quality: int = DEFAULT_QUALITY
    video_quality: str = "1080p"  # 1080p, 720p, 480p, 360p, max
    allow_quality_fallback: bool = True
    folder_format: str = DEFAULT_FOLDER
    track_format: str = DEFAULT_TRACK
    multiple_disc_track_format: str = DEFAULT_MULTIPLE_DISC_TRACK
    embed_art: bool = True
    save_cover_file: bool = True  # cover.jpg na pasta do álbum
    cover_size: int = 1280
    lyrics: bool = True  # embute letra
    save_lrc: bool = True  # grava .lrc quando há letra sincronizada
    genius_token: str = ""
    lyrics_translation_lang: str = "pt"
    no_database: bool = False
    write_sentinel: bool = True
    disable_keyring: bool = field(default_factory=is_ios)  # a-Shell não tem keyring
    concurrency: int = 2  # faixas simultâneas (2 é gentil com iPhone)
    retries: int = 3
    requests_per_minute: int = 240
    delay: float = 0.0  # pausa (s) entre faixas
    remux: str = "auto"  # auto | python | ffmpeg | none
    playlist_folder: str = "Playlists/{playlist_title}"
    accent_color: str = ""

    # -- validação ---------------------------------------------------------

    def validate(self) -> None:
        if self.quality not in QUALITY_MAP:
            raise ConfigError(f"quality inválida: {self.quality} (use 0-4)")
        if self.remux not in ("auto", "python", "ffmpeg", "none"):
            raise ConfigError(f"remux inválido: {self.remux}")
        self.concurrency = max(1, min(int(self.concurrency), 8))
        self.retries = max(1, min(int(self.retries), 10))
        for name, tpl, allowed in (
            ("folder_format", self.folder_format, FOLDER_PLACEHOLDERS),
            ("track_format", self.track_format, TRACK_PLACEHOLDERS),
            ("multiple_disc_track_format", self.multiple_disc_track_format, TRACK_PLACEHOLDERS),
        ):
            bad = validate_template(tpl, allowed)
            if bad:
                raise ConfigError(
                    f"{name} usa placeholders desconhecidos: {', '.join(bad)}. "
                    f"Válidos: {', '.join(sorted(allowed))}"
                )

    # -- config.ini --------------------------------------------------------

    @classmethod
    def from_config(cls, config_file: str) -> "TidalDLSettings":
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            cfg.read(config_file, encoding="utf-8")
        except configparser.Error as exc:
            raise ConfigError(f"config.ini ilegível: {exc}") from exc
        base = cls()
        section = SECTION if cfg.has_section(SECTION) else "DEFAULT"
        if not cfg.has_section(section) and section != "DEFAULT":
            return base
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if not cfg.has_option(section, f.name):
                continue
            raw = cfg.get(section, f.name)
            default = getattr(base, f.name)
            try:
                if isinstance(default, bool):
                    kwargs[f.name] = cfg.getboolean(section, f.name)
                elif isinstance(default, int):
                    kwargs[f.name] = int(raw)
                elif isinstance(default, float):
                    kwargs[f.name] = float(raw)
                else:
                    kwargs[f.name] = raw
            except ValueError as exc:
                raise ConfigError(f"valor inválido em [{section}] {f.name}={raw!r}") from exc
        st = cls(**kwargs)
        st.directory = os.path.expanduser(st.directory)
        st.video_directory = os.path.expanduser(st.video_directory)
        return st

    def apply_args(self, args: Any) -> "TidalDLSettings":
        """Sobrepõe por argumentos (só os que o usuário realmente passou)."""
        mapping = {
            "directory": "directory", "video_directory": "video_directory", "quality": "quality",
            "video_quality": "video_quality", "folder_format": "folder_format",
            "track_format": "track_format", "concurrency": "concurrency", "delay": "delay",
            "remux": "remux",
        }
        for arg, attr in mapping.items():
            val = getattr(args, arg, None)
            if val is not None:
                setattr(self, attr, os.path.expanduser(val) if attr in ("directory", "video_directory") else val)
        if getattr(args, "no_db", False):
            self.no_database = True
        if getattr(args, "no_sentinel", False):
            self.write_sentinel = False
        if getattr(args, "no_lyrics", False):
            self.lyrics = False
        if getattr(args, "no_fallback", False):
            self.allow_quality_fallback = False
        if getattr(args, "no_cover", False):
            self.embed_art = False
            self.save_cover_file = False
        if self.directory and is_ios() and not os.path.isabs(self.directory):
            ios_home = os.environ.get("TIDAL_DL_IOS_HOME")
            if ios_home:
                self.directory = os.path.join(ios_home, self.directory)
        if self.video_directory and is_ios() and not os.path.isabs(self.video_directory):
            ios_home = os.environ.get("TIDAL_DL_IOS_HOME")
            if ios_home:
                self.video_directory = os.path.join(ios_home, self.video_directory)
        self.validate()
        return self

    def save(self, config_file: str) -> None:
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(config_file, encoding="utf-8")
        if not cfg.has_section(SECTION):
            cfg.add_section(SECTION)
        for f in fields(self):
            val = getattr(self, f.name)
            cfg.set(SECTION, f.name, str(val).lower() if isinstance(val, bool) else str(val))
        import io

        buf = io.StringIO()
        cfg.write(buf)
        atomic_write_text(config_file, buf.getvalue(), mode=0o600)
