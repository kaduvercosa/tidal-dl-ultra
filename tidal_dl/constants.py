# ==============================================================================
# MÓDULO: constants.py (TIDAL-DL-ULTRA)
# DESCRIÇÃO: Constantes de API, qualidades e formatos padrão de nome.
#            Espelha o papel do constants.py do qobuz-dl-ultra.
# ==============================================================================

API_URL = "https://api.tidalhifi.com/v1"
LISTEN_URL = "https://listen.tidal.com/v1"
AUTH_URL = "https://auth.tidal.com/v1/oauth2"
IMAGE_URL = "https://resources.tidal.com/images"

# Qualidade: número -> valor de `audioquality` da API.
#   0 LOW  = AAC ~96 kbps        1 HIGH = AAC ~320 kbps
#   2 LOSSLESS = FLAC 16/44.1    3 HI_RES = FLAC (legado MQA, 16/44.1)
#   4 HI_RES_LOSSLESS = FLAC 24-bit até 192 kHz
QUALITY_MAP = {
    0: "LOW",
    1: "HIGH",
    2: "LOSSLESS",
    3: "HI_RES",
    4: "HI_RES_LOSSLESS",
}
QUALITY_BY_NAME = {v: k for k, v in QUALITY_MAP.items()}
QUALITY_LABELS = {
    0: "AAC 96 kbps",
    1: "AAC 320 kbps",
    2: "FLAC 16-bit/44.1 kHz",
    3: "FLAC (Hi-Res legado)",
    4: "FLAC 24-bit até 192 kHz",
}
DEFAULT_QUALITY = 4

# Nome padrão de pasta/arquivo (mesma filosofia do qobuz-dl-ultra).
# Placeholders de pasta: album_artist, album_title, year, format, bit_depth,
#   sampling_rate, album_id, release_type
# Placeholders de faixa: track_number, disc_number, track_title, track_artist,
#   album_artist, explicit, track_id
DEFAULT_FOLDER = "{release_type}/{album_artist} - {album_title} ({year}) [{format} {bit_depth}]"
DEFAULT_TRACK = "{track_number}. {track_title} {explicit}"
DEFAULT_MULTIPLE_DISC_TRACK = "{disc_number}.{track_number} - {track_title}"

FOLDER_PLACEHOLDERS = {
    "release_type", "album_artist", "album_title", "year", "format",
    "bit_depth", "sampling_rate", "album_id", "quality",
}
TRACK_PLACEHOLDERS = {
    "track_number", "disc_number", "track_title", "track_artist",
    "album_artist", "explicit", "track_id", "track_title_base",
}

# Limite de caracteres de nome de arquivo (pasta + faixa + extensão).
OK_MAX_CHARACTER_LENGTH = 180

# Marcadores de estado do álbum no NOME da pasta (mesmos do qobuz-dl-ultra;
# o `scan` entende ambos).
MARK_IN_PROGRESS = "[IN PROGRESS] "
MARK_INCOMPLETE = "[INCOMPLETE] "

# Prefixo de arquivo temporário (sem ponto: o iOS/Arquivos esconde arquivos
# que começam com "." e o a-Shell lista mal esse tipo de coisa).
TMP_PREFIX = "~tmp_"

CHUNK_SIZE = 2**17  # 128 KiB
