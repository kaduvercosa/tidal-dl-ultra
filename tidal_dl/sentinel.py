"""Sentinela por álbum (``.streamrip.json``): dedup baseado no sistema de arquivos.

ORIGEM DA IDEIA
---------------
Portado do libsync (mesmo módulo do qobuz-dl-ultra) (``backend/services/sentinels.py`` + ``scan.py``): cada pasta
de álbum baixado com sucesso recebe um pequeno JSON dizendo *qual* álbum de
*qual* serviço ela contém (Qobuz ou Tidal). Isso resolve três problemas que o banco SQLite
sozinho não resolve:

  1. Reconstruir o estado depois de ``--purge``/perda do banco (a verdade está
     no disco, junto dos arquivos).
  2. Detectar pasta movida/apagada (banco diz "baixado", disco não tem nada).
  3. Interoperar com outras ferramentas (o nome e o formato são os mesmos do
     libsync, então uma biblioteca escaneada por um serve para o outro; o
     campo ``source`` diferencia Qobuz de Tidal).

O nome do arquivo (``.streamrip.json``) é herdado do libsync de propósito --
NÃO renomeie sem renomear lá também. Escrita é atômica (tmp + ``os.replace``)
e *best-effort*: um NAS somente-leitura nunca derruba um download.

Este módulo é puro (só biblioteca padrão): não importa mutagen nem rede, então
roda em qualquer ambiente, inclusive a-Shell/iOS.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

SENTINEL_FILENAME = ".streamrip.json"
SUPPORTED_SOURCES = {"qobuz", "tidal"}

AUDIO_EXTENSIONS = {
    ".aif",
    ".aiff",
    ".alac",
    ".ape",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
}


class SentinelValidationError(ValueError):
    """A sentinela (ou a pasta dela) é insegura, inválida ou incompleta."""


@dataclass(frozen=True)
class SentinelRecord:
    """Uma sentinela válida (JSON objeto) + a pasta real onde ela mora."""

    folder: Path
    payload: dict


def utc_now_iso() -> str:
    """Timestamp ISO-8601 em UTC (com offset), estável para comparar/ordenar."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Escrita / leitura / remoção
# ---------------------------------------------------------------------------


def build_payload(
    source: str,
    album_id: Any,
    title: str,
    artist: str,
    tracks_count: Optional[int] = None,
    tracks: Optional[Iterable[dict]] = None,
    *,
    release_date: Optional[str] = None,
    quality: Optional[dict] = None,
    downloaded_at: Optional[str] = None,
) -> dict:
    """Monta o payload da sentinela (mesmas chaves do libsync).

    ``tracks`` (opcional) é uma lista de ``{"id", "title", "success", "path"}``.
    Quando presente, ``tracks_downloaded`` é derivado dela -- é isso que permite
    ao ``validate_folder`` detectar um álbum parcial.
    """
    payload: dict[str, Any] = {
        "source": str(source).strip().lower(),
        "album_id": str(album_id).strip(),
        "title": title or "",
        "artist": artist or "",
        "tracks_count": int(tracks_count) if tracks_count else None,
        "downloaded_at": downloaded_at or utc_now_iso(),
    }
    if release_date:
        payload["release_date"] = str(release_date)
    if quality:
        payload["quality"] = quality
    if tracks is not None:
        lista = [dict(t) for t in tracks]
        payload["tracks"] = lista
        payload["tracks_downloaded"] = sum(1 for t in lista if t.get("success") is True)
    return payload


def write_sentinel(folder: str | os.PathLike, payload: dict) -> bool:
    """Grava a sentinela de forma atômica. Retorna True se gravou.

    Nunca levanta: falha de permissão/FS somente-leitura só vira log, porque a
    sentinela é um *bônus* de robustez, não requisito para o download.
    """
    target = os.path.join(os.fspath(folder), SENTINEL_FILENAME)
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
        return True
    except OSError as exc:
        logger.warning("sentinela: não foi possível gravar em %s: %s", folder, exc)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def read_sentinel(folder: str | os.PathLike) -> Optional[dict]:
    """Lê a sentinela de ``folder``. None se ausente, ilegível ou não-objeto."""
    path = os.path.join(os.fspath(folder), SENTINEL_FILENAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def has_sentinel(folder: str | os.PathLike) -> bool:
    return os.path.isfile(os.path.join(os.fspath(folder), SENTINEL_FILENAME))


def remove_sentinel(folder: Optional[str | os.PathLike]) -> bool:
    """Remove a sentinela (idempotente). True se removeu algo."""
    if not folder:
        return False
    try:
        os.remove(os.path.join(os.fspath(folder), SENTINEL_FILENAME))
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("sentinela: não foi possível remover em %s: %s", folder, exc)
        return False


# ---------------------------------------------------------------------------
# Identidade
# ---------------------------------------------------------------------------


def sentinel_identity(payload: dict) -> tuple[str, str]:
    """Valida e normaliza ``(source, album_id)`` de um payload.

    Sentinelas antigas sem ``source`` são tratadas como Qobuz (compatível com o
    libsync/streamrip originais).
    """
    if "source" not in payload:
        source = "qobuz"
    else:
        raw = payload["source"]
        if not isinstance(raw, str):
            raise SentinelValidationError("Fonte da sentinela inválida")
        source = raw.strip().lower()
    if source not in SUPPORTED_SOURCES:
        raise SentinelValidationError(f"Fonte não suportada: {source or '?'}")

    raw_id = payload.get("album_id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
        raise SentinelValidationError("album_id ausente ou inválido")
    album_id = str(raw_id).strip()
    if not album_id:
        raise SentinelValidationError("album_id ausente ou inválido")
    return source, album_id


def sentinel_downloaded_at(payload: dict) -> str:
    """Preserva timestamp válido da sentinela; senão usa 'agora' (UTC)."""
    raw = payload.get("downloaded_at")
    if isinstance(raw, str):
        value = raw.strip()
        try:
            datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    return utc_now_iso()


# ---------------------------------------------------------------------------
# Descoberta segura
# ---------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def discover_sentinels(
    download_root: str | os.PathLike, *, max_depth: int = 4
) -> tuple[list[SentinelRecord], list[dict], int]:
    """Varre ``download_root`` e devolve ``(registros, falhas, total_visto)``.

    Segurança (mesmas garantias do libsync):
      * nunca segue symlink de diretório;
      * recusa symlink que aponte para FORA da raiz;
      * recusa sentinela que seja symlink;
      * JSON inválido/malformado vira uma *falha* individual -- uma pasta ruim
        não aborta o resto da varredura.
    """
    try:
        root = Path(download_root).resolve(strict=True)
    except FileNotFoundError:
        return [], [], 0
    except OSError as exc:
        return [], [{"folder": str(download_root), "error": str(exc)}], 0
    if not root.is_dir():
        return [], [], 0

    records: list[SentinelRecord] = []
    failures: list[dict] = []
    scanned = 0

    def walk_error(error: OSError) -> None:
        failures.append({"folder": error.filename or str(root), "error": str(error)})

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        folder = Path(current)
        try:
            actual = folder.resolve(strict=True)
            depth = len(actual.relative_to(root).parts)
        except (OSError, ValueError) as exc:
            dirs[:] = []
            failures.append({"folder": str(folder), "error": str(exc)})
            continue

        kept = []
        for name in dirs:
            candidate = folder / name
            if candidate.is_symlink():
                try:
                    target = candidate.resolve(strict=True)
                except OSError as exc:
                    failures.append({"folder": str(candidate), "error": str(exc)})
                    scanned += 1
                    continue
                if not _inside(target, root):
                    failures.append(
                        {
                            "folder": str(candidate),
                            "error": "Symlink aponta para fora da raiz; ignorado",
                        }
                    )
                    scanned += 1
                continue
            kept.append(name)
        dirs[:] = kept if depth < max_depth else []

        if SENTINEL_FILENAME not in files:
            continue
        scanned += 1
        sentinel = folder / SENTINEL_FILENAME
        if sentinel.is_symlink():
            failures.append(
                {"folder": str(actual), "error": "Sentinela é um symlink; recusada"}
            )
            continue
        try:
            with sentinel.open(encoding="utf-8") as fh:
                payload = json.load(fh)
            if not isinstance(payload, dict):
                raise SentinelValidationError("O JSON da sentinela deve ser um objeto")
        except (OSError, UnicodeError, json.JSONDecodeError, SentinelValidationError) as exc:
            failures.append(
                {"folder": str(actual), "error": f"Sentinela inválida: {exc}"}
            )
            continue
        records.append(SentinelRecord(folder=actual, payload=payload))

    return records, failures, scanned


# ---------------------------------------------------------------------------
# Validação da pasta contra a sentinela
# ---------------------------------------------------------------------------


def count_audio_files(folder: str | os.PathLike, *, max_depth: int = 2) -> int:
    """Conta arquivos de áudio (não-symlink) na pasta e em ``Disc N/`` abaixo."""
    root = Path(folder)
    total = 0
    for current, dirs, names in os.walk(root, followlinks=False):
        cur = Path(current)
        try:
            depth = len(cur.relative_to(root).parts)
        except ValueError:
            continue
        dirs[:] = [
            d for d in dirs if not (cur / d).is_symlink()
        ] if depth < max_depth else []
        for name in names:
            p = cur / name
            if p.suffix.lower() in AUDIO_EXTENSIONS and not p.is_symlink():
                total += 1
    return total


def _positive_int(payload: dict, key: str) -> Optional[int]:
    if key not in payload or payload[key] is None:
        return None
    raw = payload[key]
    if isinstance(raw, bool):
        raise SentinelValidationError(f"{key} inválido")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SentinelValidationError(f"{key} inválido") from None
    if value <= 0:
        raise SentinelValidationError(f"{key} inválido")
    return value


def validate_folder(folder: str | os.PathLike, payload: dict) -> list[str]:
    """Confere se a pasta bate com a sentinela. Retorna a lista de problemas.

    Lista vazia == pasta consistente. Os problemas são textos prontos para
    exibir no terminal (usados por ``library reconcile`` e ``doctor``).
    """
    problemas: list[str] = []
    try:
        expected = _positive_int(payload, "tracks_count")
        downloaded = _positive_int(payload, "tracks_downloaded")
    except SentinelValidationError as exc:
        return [str(exc)]

    found = count_audio_files(folder)
    if expected is not None and found != expected:
        problemas.append(f"esperava {expected} arquivos de áudio, achou {found}")
    if expected is not None and downloaded is not None and downloaded != expected:
        problemas.append(
            f"sentinela registra {downloaded}/{expected} faixas baixadas (parcial)"
        )
    tracks = payload.get("tracks")
    if tracks is not None:
        if not isinstance(tracks, list):
            problemas.append("campo 'tracks' não é uma lista")
        else:
            ok_ids = [
                str(t.get("id")).strip()
                for t in tracks
                if isinstance(t, dict) and t.get("success") is True
            ]
            if len(set(ok_ids)) != len(ok_ids):
                problemas.append("IDs de faixa duplicados na sentinela")
            if expected is not None and len(ok_ids) != expected:
                problemas.append(
                    f"só {len(ok_ids)} de {expected} faixas marcadas como sucesso"
                )
    return problemas
