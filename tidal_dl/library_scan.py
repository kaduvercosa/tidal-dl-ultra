"""Scan da biblioteca: casa pastas de álbum do disco com o catálogo (``library.db``).

ORIGEM DA IDEIA
---------------
Portado do ``backend/services/scan.py`` do libsync (normalização, classificação
auto/revisar/sem-match, sentinela e primitivas *mark/unmark*), com melhorias
pensadas para a realidade do tidal-dl-ultra (espelha o qobuz-dl-ultra):

  * **Match por ID de tag primeiro.** Arquivos baixados por este projeto trazem
    ``TIDALALBUMID``/``BARCODE``. Se a tag existe, o match é *certo* (não
    depende de nome). O libsync só tinha match por nome.
  * **Match por UPC** (``BARCODE``) contra o catálogo.
  * **Fuzzy de verdade** (via ``tidal_dl.fuzzy``, que usa rapidfuzz se houver e
    difflib se não): candidatos "quase iguais" vão para *revisão*, nunca
    são auto-marcados.
  * **Estados do downloader**: pastas ``[INCOMPLETE]``/``[IN PROGRESS]`` nunca
    viram "completas" -- viram status ``incomplete``.
  * **Multi-disco**: ``Álbum/CD 01`` + ``Álbum/CD 02`` é UM álbum (o libsync
    gerava um candidato por disco).
  * **Adoção**: pasta com tag de ID que não está nos favoritos entra no
    catálogo como álbum "fora dos favoritos" (evita re-baixar o que você já
    tem mas não favoritou).

Regras de segurança herdadas: nunca auto-marca quando há mais de um candidato,
quando o profundidade de bits diverge ou quando a contagem de faixas diverge
(pasta com faixas faltando é download interrompido; marcar como completo
impediria baixar o resto).

O módulo lê tags com ``mutagen`` de forma *lazy* (função ``_read_tags``), então
a lógica de matching pode ser testada sem mutagen instalado.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from tidal_dl import fuzzy
from tidal_dl.library_db import (
    STATUS_INCOMPLETE,
    LibraryDB,
)
from tidal_dl.sentinel import (
    AUDIO_EXTENSIONS,
    build_payload,
    has_sentinel,
    remove_sentinel,
    write_sentinel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normalização
# ---------------------------------------------------------------------------

_PAREN_SUFFIX_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_LEADING_THE_RE = re.compile(r"^the\s+", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
# Marcadores de estado que o downloader coloca no NOME da pasta.
_STATE_PREFIX_RE = re.compile(r"^\s*\[(IN PROGRESS|INCOMPLETE)\]\s*", re.IGNORECASE)
# "CD 01", "Disc 2", "Disco 3", "Disk 1", "DVD 1"
_DISC_DIR_RE = re.compile(r"^(?:cd|dis[ck]o?|dvd)[\s._-]*\d+\b", re.IGNORECASE)


def normalize(value: Optional[str]) -> str:
    """Dobra um nome de artista/álbum numa chave estável de comparação.

    casefold, remove marcador de estado do downloader, remove sufixos
    ``(…)``/``[…]`` repetidamente ("X (Deluxe) [FLAC 24]" → "x"), NFKD sem
    diacríticos, remove um "The " inicial e colapsa espaços.
    """
    if not value:
        return ""
    s = _STATE_PREFIX_RE.sub("", value).casefold()
    while True:
        stripped = _PAREN_SUFFIX_RE.sub("", s)
        if stripped == s:
            break
        s = stripped
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _LEADING_THE_RE.sub("", s, count=1)
    return _WHITESPACE_RE.sub(" ", s).strip()


def folder_state(name: str) -> str:
    """``"in_progress"`` | ``"incomplete"`` | ``"ok"`` a partir do nome da pasta."""
    m = _STATE_PREFIX_RE.match(name or "")
    if not m:
        return "ok"
    return "in_progress" if m.group(1).upper() == "IN PROGRESS" else "incomplete"


def is_disc_folder(name: str) -> bool:
    return bool(_DISC_DIR_RE.match(name or ""))


# ---------------------------------------------------------------------------
# Leitura de pasta
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FolderMeta:
    """Metadados extraídos de uma pasta de álbum."""

    folder: Path
    artist: str
    album: str
    bit_depth: Optional[int]
    sample_rate: Optional[float]
    track_count: int
    source: str  # "tags" | "folder_name"
    state: str = "ok"  # "ok" | "incomplete" | "in_progress"
    service_album_id: Optional[str] = None  # TIDALALBUMID (nunca lemos IDs de outro serviço)
    upc: Optional[str] = None  # BARCODE


def _is_audio(p: Path) -> bool:
    return p.is_file() and not p.is_symlink() and p.suffix.lower() in AUDIO_EXTENSIONS


def audio_files(folder: Path) -> list[Path]:
    """Áudios da pasta; se ela só tem subpastas ``CD N``, junta os dos discos."""
    direct = sorted(p for p in folder.iterdir() if _is_audio(p))
    if direct:
        return direct
    out: list[Path] = []
    for d in sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink()):
        if is_disc_folder(d.name):
            out.extend(sorted(p for p in d.iterdir() if _is_audio(p)))
    return out


def _first(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    text = getattr(value, "text", None)  # frames ID3
    if text:
        value = text[0]
    value = str(value).strip()
    return value or None


def _read_tags(path: Path) -> dict:
    """Lê artista/álbum/bit-depth/sample-rate + IDs de serviço de UM arquivo.

    Devolve ``{}`` se o mutagen não estiver instalado ou o arquivo for ilegível.
    Isolado numa função para os testes poderem substituí-la.
    """
    try:
        import mutagen
    except ImportError:  # pragma: no cover - mutagen é dependência do projeto
        return {}
    out: dict[str, Any] = {}
    try:
        easy = mutagen.File(str(path), easy=True)
    except Exception:
        easy = None
    if easy is not None:
        tags = getattr(easy, "tags", None)
        if tags is not None:
            out["artist"] = _first(tags.get("albumartist")) or _first(tags.get("artist"))
            out["album"] = _first(tags.get("album"))
        info = getattr(easy, "info", None)
        if info is not None:
            bps = getattr(info, "bits_per_sample", None)
            if isinstance(bps, int) and bps > 0:
                out["bit_depth"] = bps
            sr = getattr(info, "sample_rate", None)
            if isinstance(sr, (int, float)) and sr > 0:
                out["sample_rate"] = round(sr / 1000, 1)
    # IDs de serviço gravados pelo downloader (não existem no modo "easy").
    try:
        ext = path.suffix.lower()
        if ext == ".flac":
            from mutagen.flac import FLAC

            f = FLAC(str(path))
            out["service_album_id"] = _first(
                f.get("TIDALALBUMID")
            )
            out["upc"] = _first(f.get("BARCODE") or f.get("UPC"))
        elif ext == ".mp3":
            from mutagen.id3 import ID3

            t = ID3(str(path))
            out["service_album_id"] = _first(
                t.get("TXXX:TIDALALBUMID")
            )
            out["upc"] = _first(t.get("TXXX:BARCODE") or t.get("TXXX:UPC"))
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4

            m = MP4(str(path))

            def _ff(key):
                vals = m.tags.get(f"----:com.apple.iTunes:{key}") if m.tags else None
                return _first(bytes(vals[0]).decode("utf-8", "replace")) if vals else None

            out["service_album_id"] = _ff("TIDALALBUMID")
            out["upc"] = _ff("BARCODE")
    except Exception:
        pass
    return out


def _parse_folder_name(name: str) -> tuple[Optional[str], str]:
    """``"Artista - Álbum (2019) [FLAC 24]"`` → ``("Artista", "Álbum (2019) [FLAC 24]")``."""
    name = _STATE_PREFIX_RE.sub("", name).strip()
    if " - " in name:
        artist, album = name.split(" - ", 1)
        return artist.strip(), album.strip()
    return None, name


def read_folder_metadata(folder: Path) -> Optional[FolderMeta]:
    """Extrai metadados de uma pasta. None se não houver áudio."""
    files = audio_files(folder)
    if not files:
        return None
    tags = _read_tags(files[0])
    artist = tags.get("artist")
    album = tags.get("album")
    source = "tags" if (artist and album) else "folder_name"
    if not album:
        p_artist, p_album = _parse_folder_name(folder.name)
        album = p_album
        artist = artist or p_artist
    if not album:
        return None
    return FolderMeta(
        folder=folder,
        artist=artist or "",
        album=album,
        bit_depth=tags.get("bit_depth"),
        sample_rate=tags.get("sample_rate"),
        track_count=len(files),
        source=source,
        state=folder_state(folder.name),
        service_album_id=tags.get("service_album_id"),
        upc=tags.get("upc"),
    )


# ---------------------------------------------------------------------------
# Índice e classificação
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    album_id: int
    source: str
    artist: str
    title: str
    score: float
    reason: str


@dataclass(frozen=True)
class MatchResult:
    kind: str  # "auto_match" | "review" | "unmatched"
    album_id: Optional[int] = None
    reason: str = ""
    candidates: tuple[Candidate, ...] = ()


@dataclass
class LibraryIndex:
    by_full_key: dict[tuple[str, str], list[dict]] = field(default_factory=dict)
    by_album_only: dict[str, list[dict]] = field(default_factory=dict)
    by_artist: dict[str, list[dict]] = field(default_factory=dict)
    by_source_id: dict[tuple[str, str], dict] = field(default_factory=dict)
    by_upc: dict[str, list[dict]] = field(default_factory=dict)


def build_library_index(albums: list[dict]) -> LibraryIndex:
    full: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_album: dict[str, list[dict]] = defaultdict(list)
    by_artist: dict[str, list[dict]] = defaultdict(list)
    by_id: dict[tuple[str, str], dict] = {}
    by_upc: dict[str, list[dict]] = defaultdict(list)
    for a in albums:
        na, nt = normalize(a["artist"]), normalize(a["title"])
        full[(na, nt)].append(a)
        by_album[nt].append(a)
        by_artist[na].append(a)
        by_id[(a["source"], str(a["source_album_id"]))] = a
        upc = (a.get("upc") or "").strip().lstrip("0")
        if upc:
            by_upc[upc].append(a)
    return LibraryIndex(dict(full), dict(by_album), dict(by_artist), by_id, dict(by_upc))


def _bit_depth_matches(local: Optional[int], library: Optional[int]) -> bool:
    """Desconhecido de um dos lados é tratado como 'compatível'."""
    if local is None or library is None:
        return True
    return local == library


def _track_count_matches(local: Optional[int], library: Optional[int]) -> bool:
    """Contagem desconhecida (None/0) é 'compatível'.

    Pasta com número de faixas diferente do álbum quase sempre é download
    interrompido; auto-marcar impediria baixar as faltantes -- vai p/ revisão.
    """
    if not local or not library:
        return True
    return local == library


def _compat(meta: FolderMeta, album: dict) -> list[str]:
    reasons = []
    if not _bit_depth_matches(meta.bit_depth, album.get("bit_depth")):
        reasons.append(
            f"bit_depth_mismatch: local={meta.bit_depth} catalogo={album.get('bit_depth')}"
        )
    if not _track_count_matches(meta.track_count, album.get("track_count")):
        reasons.append(
            f"track_count_mismatch: local={meta.track_count} catalogo={album.get('track_count')}"
        )
    return reasons


def _cand(album: dict, score: float, reason: str) -> Candidate:
    return Candidate(album["id"], album["source"], album["artist"], album["title"], score, reason)


def classify(
    meta: FolderMeta,
    index: LibraryIndex,
    *,
    source: str = "tidal",
    fuzzy_threshold: float = 0.85,
) -> MatchResult:
    """Classifica UMA pasta contra o catálogo.

    Ordem: ID de tag → UPC → nome exato normalizado → fuzzy (só revisão).
    Pasta ``[INCOMPLETE]``/``[IN PROGRESS]`` nunca é ``auto_match``.
    """
    can_auto = meta.state == "ok"

    # 1) ID de serviço gravado na tag: identidade certa.
    if meta.service_album_id:
        album = index.by_source_id.get((source, str(meta.service_album_id)))
        if album is not None:
            reasons = _compat(meta, album)
            if not reasons and can_auto:
                return MatchResult("auto_match", album["id"], "tag_id")
            if not can_auto:
                reasons.append(f"pasta_{meta.state}")
            return MatchResult(
                "review", candidates=(_cand(album, 0.99, "; ".join(reasons) or "tag_id"),)
            )

    # 2) UPC (BARCODE).
    if meta.upc:
        hits = index.by_upc.get(meta.upc.strip().lstrip("0"), [])
        hits = [h for h in hits if h["source"] == source] or hits
        if len(hits) == 1:
            album = hits[0]
            reasons = _compat(meta, album)
            if not reasons and can_auto:
                return MatchResult("auto_match", album["id"], "upc")
            if not can_auto:
                reasons.append(f"pasta_{meta.state}")
            return MatchResult(
                "review", candidates=(_cand(album, 0.97, "; ".join(reasons) or "upc"),)
            )

    # 3) Nome exato normalizado.
    na, nt = normalize(meta.artist), normalize(meta.album)
    if na:
        candidates = index.by_full_key.get((na, nt), [])
    else:
        candidates = index.by_album_only.get(nt, [])

    if candidates:
        compatible = [a for a in candidates if not _compat(meta, a)]
        # Só auto-marca com UM candidato no total, UM compatível, artista
        # confiável e pasta em estado normal.
        if len(candidates) == 1 and len(compatible) == 1 and na and can_auto:
            return MatchResult("auto_match", compatible[0]["id"], "exact")
        out = []
        for album in candidates:
            reasons = []
            if not na:
                reasons.append("missing_artist")
            reasons.extend(_compat(meta, album))
            if not can_auto:
                reasons.append(f"pasta_{meta.state}")
            if len(candidates) > 1 and not reasons:
                reasons.append("multiple_candidates")
            if not reasons:
                reasons.append("ambiguous")
            out.append(_cand(album, 0.9 if na else 0.6, "; ".join(reasons)))
        return MatchResult("review", candidates=tuple(out))

    # 4) Fuzzy (sempre revisão).
    fuzzy_hits: dict[int, Candidate] = {}
    if na:
        for album in index.by_artist.get(na, []):
            r = fuzzy.ratio(nt, normalize(album["title"]))
            if r >= fuzzy_threshold:
                fuzzy_hits[album["id"]] = _cand(album, round(r, 3), f"fuzzy: titulo={r:.2f}")
    for album in index.by_album_only.get(nt, []):
        r = fuzzy.ratio(na, normalize(album["artist"])) if na else 0.0
        if r >= fuzzy_threshold and album["id"] not in fuzzy_hits:
            fuzzy_hits[album["id"]] = _cand(album, round(r, 3), f"fuzzy: artista={r:.2f}")
    if fuzzy_hits:
        ranked = sorted(fuzzy_hits.values(), key=lambda c: c.score, reverse=True)[:5]
        return MatchResult("review", candidates=tuple(ranked))

    return MatchResult("unmatched")


# ---------------------------------------------------------------------------
# Descoberta de pastas
# ---------------------------------------------------------------------------


def find_album_folders(root: Path, max_depth: int = 4) -> tuple[list[Path], list[str]]:
    """Acha pastas que parecem álbuns (têm áudio direto ou só ``CD N`` com áudio).

    Uma pasta com áudio direto é álbum e NÃO é aprofundada. Multi-disco vira
    UM álbum (a pasta-pai). Symlinks e pastas ocultas são ignorados; diretório
    ilegível vai para ``skipped`` sem abortar a varredura.
    """
    results: list[Path] = []
    skipped: list[str] = []

    def walk(folder: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            children = list(folder.iterdir())
        except OSError as exc:
            logger.warning("scan: pulando pasta ilegível %s: %s", folder, exc)
            skipped.append(str(folder))
            return
        if any(_is_audio(p) for p in children):
            results.append(folder)
            return
        subdirs = []
        for child in sorted(children):
            if child.is_symlink():
                skipped.append(str(child))
                continue
            if child.is_dir() and not child.name.startswith("."):
                subdirs.append(child)
        discs = [d for d in subdirs if is_disc_folder(d.name)]
        if discs and len(discs) == len(subdirs):
            try:
                if any(any(_is_audio(p) for p in d.iterdir()) for d in discs):
                    results.append(folder)
                    return
            except OSError:
                skipped.append(str(folder))
                return
        for child in subdirs:
            walk(child, depth + 1)

    walk(root, 0)
    return sorted(results), skipped


# ---------------------------------------------------------------------------
# Primitivas mark / unmark
# ---------------------------------------------------------------------------


def mark_album_downloaded(
    lib: LibraryDB,
    album_id: int,
    *,
    folder: str | os.PathLike,
    sentinel_enabled: bool = True,
    downloaded_at: Optional[str] = None,
    tracks: Optional[list[dict]] = None,
) -> dict:
    """Marca o álbum como completo no catálogo (+ sentinela best-effort).

    Idempotente. Devolve a linha atualizada.
    """
    lib.set_download_state(
        album_id,
        downloaded=True,
        downloaded_at=downloaded_at,
        local_folder_path=str(folder),
    )
    album = lib.get_album(album_id)
    assert album is not None
    if sentinel_enabled:
        write_sentinel(
            folder,
            build_payload(
                album["source"],
                album["source_album_id"],
                album["title"],
                album["artist"],
                album.get("track_count"),
                tracks,
                release_date=album.get("release_date"),
                downloaded_at=album.get("downloaded_at"),
            ),
        )
    return album


def unmark_album_downloaded(lib: LibraryDB, album_id: int, *, remove_files: bool = True) -> dict:
    """Desmarca o álbum e remove a sentinela da pasta (se houver)."""
    old = lib.set_download_state(album_id, downloaded=False)
    if remove_files:
        remove_sentinel(old.get("local_folder_path"))
    return old


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------

DedupWriter = Callable[[dict, Path, FolderMeta], Awaitable[None]]
ProgressCb = Callable[[int, int, Path], None]


def _review_item(meta: FolderMeta, result: MatchResult) -> dict:
    return {
        "folder": str(meta.folder),
        "local_artist": meta.artist,
        "local_album": meta.album,
        "local_bit_depth": meta.bit_depth,
        "local_sample_rate": meta.sample_rate,
        "local_track_count": meta.track_count,
        "candidates": [
            {
                "album_id": c.album_id,
                "source": c.source,
                "artist": c.artist,
                "title": c.title,
                "score": c.score,
                "reason": c.reason,
            }
            for c in result.candidates
        ],
    }


async def run_scan(
    lib: LibraryDB,
    root: str | os.PathLike,
    *,
    source: str = "tidal",
    apply: bool = True,
    sentinel_enabled: bool = True,
    adopt_unknown: bool = True,
    rescan: bool = False,
    max_depth: int = 4,
    fuzzy_threshold: float = 0.85,
    dedup_writer: Optional[DedupWriter] = None,
    progress: Optional[ProgressCb] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> dict:
    """Varre ``root``, classifica cada pasta e (se ``apply``) marca os matches certos.

    ``apply=False`` é o *dry-run*: classifica e reporta, não grava nada.
    Trabalho de disco/mutagen roda em thread para não travar o event loop.
    """
    report: dict[str, Any] = {
        "status": "complete",
        "root": str(root),
        "scanned": 0,
        "sentinel_skipped": 0,
        "skipped_dirs": [],
        "auto_matched": [],
        "adopted": [],
        "incomplete": [],
        "review": [],
        "unmatched": [],
        "failed": [],
        "dry_run": not apply,
    }
    root_path = Path(root)
    if not root_path.is_dir():
        report["status"] = "root_not_found"
        return report

    index = build_library_index(lib.get_all_albums_for_index())
    folders, skipped = await asyncio.to_thread(find_album_folders, root_path, max_depth)
    report["skipped_dirs"] = skipped
    total = len(folders)

    for i, folder in enumerate(folders, start=1):
        if stop_event is not None and stop_event.is_set():
            report["status"] = "interrupted"
            break
        report["scanned"] = i
        if progress:
            progress(i, total, folder)

        if not rescan and await asyncio.to_thread(has_sentinel, folder):
            report["sentinel_skipped"] += 1
            continue
        meta = await asyncio.to_thread(read_folder_metadata, folder)
        if meta is None:
            continue

        result = classify(meta, index, source=source, fuzzy_threshold=fuzzy_threshold)

        try:
            if result.kind == "auto_match" and result.album_id is not None:
                entry = {
                    "album_id": result.album_id,
                    "folder": str(folder),
                    "reason": result.reason,
                }
                if apply:
                    album = await asyncio.to_thread(
                        mark_album_downloaded,
                        lib,
                        result.album_id,
                        folder=folder,
                        sentinel_enabled=sentinel_enabled,
                    )
                    if dedup_writer is not None:
                        await dedup_writer(album, folder, meta)
                report["auto_matched"].append(entry)

            elif result.kind == "review":
                # Pasta interrompida com match por ID certo: registra o estado.
                if meta.state != "ok" and len(result.candidates) == 1:
                    cand = result.candidates[0]
                    report["incomplete"].append(
                        {"album_id": cand.album_id, "folder": str(folder),
                         "state": meta.state, "title": cand.title, "artist": cand.artist}
                    )
                    if apply:
                        await asyncio.to_thread(lib.update_status, cand.album_id, STATUS_INCOMPLETE)
                        await asyncio.to_thread(lib.set_local_folder, cand.album_id, str(folder))
                else:
                    report["review"].append(_review_item(meta, result))

            else:  # unmatched
                if adopt_unknown and meta.service_album_id and meta.state == "ok":
                    entry = {
                        "source": source,
                        "source_album_id": meta.service_album_id,
                        "folder": str(folder),
                        "title": meta.album,
                        "artist": meta.artist,
                    }
                    if apply:
                        new_id = await asyncio.to_thread(
                            lib.upsert_album,
                            source,
                            meta.service_album_id,
                            meta.album,
                            meta.artist,
                            track_count=meta.track_count,
                            bit_depth=meta.bit_depth,
                            sample_rate=meta.sample_rate,
                            upc=meta.upc,
                        )
                        album = await asyncio.to_thread(
                            mark_album_downloaded,
                            lib,
                            new_id,
                            folder=folder,
                            sentinel_enabled=sentinel_enabled,
                        )
                        if dedup_writer is not None:
                            await dedup_writer(album, folder, meta)
                    report["adopted"].append(entry)
                else:
                    report["unmatched"].append(str(folder))
        except Exception as exc:  # uma pasta ruim não derruba o scan inteiro
            logger.exception("scan: falha ao processar %s", folder)
            report["failed"].append({"folder": str(folder), "error": str(exc)})

    return report


def apply_review_choice(
    lib: LibraryDB,
    item: dict,
    album_id: int,
    *,
    sentinel_enabled: bool = True,
) -> dict:
    """Aplica a escolha manual do usuário para um item de revisão."""
    return mark_album_downloaded(
        lib, album_id, folder=item["folder"], sentinel_enabled=sentinel_enabled
    )


# ---------------------------------------------------------------------------
# Reconciliação de sentinelas (disco ⇄ catálogo)
# ---------------------------------------------------------------------------


def reconcile_sentinels(
    lib: LibraryDB,
    root: str | os.PathLike,
    *,
    apply: bool = True,
    fix_missing: bool = False,
    max_depth: int = 4,
) -> dict:
    """Confronta as sentinelas do disco com o catálogo.

    * sentinela válida + pasta consistente → álbum marcado ``complete``
      (criado no catálogo se ainda não existir);
    * sentinela com problema (faixas faltando, JSON ruim…) → ``invalid``;
    * catálogo diz ``complete`` mas a pasta sumiu → ``missing_on_disk``
      (com ``fix_missing`` o álbum volta a ``not_downloaded``).
    """
    from tidal_dl.sentinel import (
        SentinelValidationError,
        discover_sentinels,
        sentinel_downloaded_at,
        sentinel_identity,
        validate_folder,
    )

    records, failures, scanned = discover_sentinels(root, max_depth=max_depth)
    report: dict[str, Any] = {
        "scanned": scanned,
        "reconciled": [],
        "invalid": list(failures),
        "missing_on_disk": [],
        "dry_run": not apply,
    }
    seen: set[tuple[str, str]] = set()
    for rec in records:
        try:
            source, sid = sentinel_identity(rec.payload)
            problemas = validate_folder(rec.folder, rec.payload)
            if problemas:
                report["invalid"].append(
                    {"folder": str(rec.folder), "source": source, "album_id": sid,
                     "error": "; ".join(problemas)}
                )
                continue
            seen.add((source, sid))
            if apply:
                title = str(rec.payload.get("title") or "").strip() or "Unknown"
                artist = str(rec.payload.get("artist") or "").strip() or "Unknown"
                count = rec.payload.get("tracks_count")
                album_id = lib.upsert_album(
                    source, sid, title, artist,
                    track_count=int(count) if isinstance(count, int) and count > 0 else None,
                    release_date=rec.payload.get("release_date"),
                )
                lib.set_download_state(
                    album_id,
                    downloaded=True,
                    downloaded_at=sentinel_downloaded_at(rec.payload),
                    local_folder_path=str(rec.folder),
                )
            report["reconciled"].append({"source": source, "album_id": sid, "folder": str(rec.folder)})
        except (SentinelValidationError, ValueError) as exc:
            report["invalid"].append({"folder": str(rec.folder), "error": str(exc)})

    for a in lib.get_albums(status="complete"):
        folder = a.get("local_folder_path")
        if folder and os.path.isdir(folder):
            continue
        if (a["source"], str(a["source_album_id"])) in seen:
            continue
        report["missing_on_disk"].append(
            {"album_id": a["id"], "source": a["source"], "artist": a["artist"],
             "title": a["title"], "folder": folder or None}
        )
        if apply and fix_missing:
            lib.set_download_state(a["id"], downloaded=False)
    return report
