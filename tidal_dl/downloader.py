"""Downloader do tidal-dl-ultra: álbuns, faixas e playlists.

FLUXO DE UM ÁLBUM
-----------------
1. busca álbum + faixas; se já está no banco (e a pasta existe) -> pula;
2. pasta de trabalho ``[IN PROGRESS] Nome`` (mesmo padrão do qobuz-dl-ultra);
3. baixa capa uma vez; baixa as faixas em sequência (barra de progresso) ou em paralelo (``max_workers``);
4. por faixa: resolve stream (com fallback de qualidade) -> baixa para
   ``~tmp_`` -> remux (DASH/FLAC) -> letra -> tags -> renomeia atomicamente;
5. tudo ok  -> renomeia a pasta para o nome final, grava sentinela e banco;
   com falha -> ``[INCOMPLETE] Nome`` (o `scan` entende esse estado e uma nova
   execução retoma: faixas prontas são puladas).

Nada aqui usa ffmpeg obrigatoriamente: o remux FLAC-em-MP4 é Python puro
(``fmp4``), o que importa no a-Shell.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Optional

from tidal_dl import db as dbm
from tidal_dl import fmp4, lyrics as lyr, metadata, progress, sentinel, ui
from tidal_dl.color import GREEN, INFO as CYAN, RED, RESET, YELLOW
from tidal_dl.constants import (
    CHUNK_SIZE,
    MARK_INCOMPLETE,
    MARK_IN_PROGRESS,
    OK_MAX_CHARACTER_LENGTH,
    QUALITY_BY_NAME,
    QUALITY_MAP,
    TMP_PREFIX,
)
from tidal_dl.exceptions import (
    AuthenticationError,
    DownloadError,
    NonStreamable,
    PermanentDownloadError,
    PreviewOnly,
    ResourceNotFoundError,
)
from tidal_dl import hls
from tidal_dl.manifest import resolve_stream, resolve_video_playback
from tidal_dl.models import Album, Stream, Track, Video
from tidal_dl.net import NetworkError
from tidal_dl.settings import TidalDLSettings
from tidal_dl.utils import (
    cover_url,
    encontrar_binario,
    human_size,
    remove_quiet,
    render_path,
    render_template,
    retry_async,
    sanitize_component,
    truncate_name,
)

logger = logging.getLogger(__name__)

# Erros que abortam o álbum inteiro (não adianta seguir para a próxima faixa).
FATAL = (AuthenticationError, PreviewOnly)


@dataclass
class TrackResult:
    track_id: int
    title: str
    success: bool = False
    skipped: bool = False
    path: str = ""
    error: str = ""
    quality: str = ""
    file_format: str = ""
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None


@dataclass
class AlbumResult:
    album_id: Any
    title: str = ""
    artist: str = ""
    folder: str = ""
    skipped: bool = False
    tracks: list[TrackResult] = field(default_factory=list)

    @property
    def successful(self) -> int:
        return sum(1 for t in self.tracks if t.success)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tracks if not t.success)

    @property
    def ok(self) -> bool:
        return self.skipped or (bool(self.tracks) and self.failed == 0)


def quality_fields(album_quality: str, cfg_quality: int) -> tuple[str, Optional[int], str]:
    """``(formato, bit_depth, sampling_rate)`` para o nome da pasta.

    O tier efetivo é o MENOR entre o que o álbum tem e o que o usuário pediu:
    pedir HIGH num álbum Hi-Res gera arquivos AAC, e a pasta não pode dizer FLAC.
    """
    cfg = max(0, min(int(cfg_quality), 4))
    rank = QUALITY_BY_NAME.get((album_quality or "").upper(), cfg)
    name = QUALITY_MAP[min(cfg, rank)]
    if name in ("LOW", "HIGH"):
        return "AAC", 16, "44.1"
    if name in ("LOSSLESS", "HI_RES"):
        return "FLAC", 16, "44.1"
    return "FLAC", 24, ""


async def run_limited(coros: list, limit: int) -> list:
    """Roda corrotinas com no máximo ``limit`` simultâneas, preservando a ordem.

    Uma exceção FATAL cancela as demais e é relevantada; as outras exceções
    devem ser tratadas dentro da corrotina.
    """
    sem = asyncio.Semaphore(max(1, limit))

    async def guarded(c):
        async with sem:
            return await c

    tasks = [asyncio.ensure_future(guarded(c)) for c in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


class Downloader:
    def __init__(
        self,
        api: Any,
        settings: TidalDLSettings,
        *,
        db_path: Optional[str] = None,
        sleep=asyncio.sleep,
    ):
        self.api = api
        self.settings = settings
        self.db_path = None if settings.no_database else db_path
        self._sleep = sleep
        self._album_cache: dict[int, Album] = {}
        self._cover_cache: dict[str, Optional[bytes]] = {}
        self._warned_tags = False
        self._warned_remux = False
        self._parallel = False
        self._workers = 1

    # ------------------------------------------------------------------
    # Rede: arquivo simples e segmentos
    # ------------------------------------------------------------------

    async def _fetch_bytes(self, url: str) -> Optional[bytes]:
        try:
            resp = await self.api.http.request("GET", url)
        except NetworkError:
            return None
        return resp.content if resp.status < 400 and resp.content else None

    # ------------------------------------------------------------------
    # Modo de execução (sequencial x paralelo)
    # ------------------------------------------------------------------

    def _configure_mode(self, track_count: int) -> str:
        """Define workers/paralelismo e devolve o rótulo mostrado no cabeçalho.

        Mesma regra do qobuz-dl-ultra: ``--delay`` força sequencial ("Safety
        Delay"); paralelo só faz sentido com mais de uma faixa.
        """
        workers = int(self.settings.max_workers)
        self._parallel = False
        label = "Sequencial"
        if self.settings.delay > 0:
            workers = 1
            label = "Sequencial (Safety Delay ativo)"
        elif workers > 1 and track_count > 1:
            self._parallel = True
            label = f"Paralelo ({workers} workers)"
        else:
            workers = 1
        self._workers = workers
        return label

    # ------------------------------------------------------------------
    # Rede: BTS (arquivo único, com resume por Range) e DASH (segmentos)
    # ------------------------------------------------------------------

    @staticmethod
    def _hdr(headers: dict, name: str) -> str:
        lname = name.lower()
        for k, v in (headers or {}).items():
            if k.lower() == lname:
                return str(v)
        return ""

    async def _bts_attempt(self, url: str, tmp: str, state: dict, name: str, bar_on: bool) -> None:
        """Uma tentativa de baixar o arquivo único, retomando de ``state['bytes']``."""
        headers = {"Range": f"bytes={state['bytes']}-"} if state["bytes"] else None
        async with self.api.http.stream(url, headers=headers) as r:
            if r.status in (401, 403, 404, 451):
                raise PermanentDownloadError(f"HTTP {r.status}: áudio indisponível (região, direitos ou sessão)")
            if r.status == 416:
                total = state.get("total", 0)
                if state["bytes"] and state["bytes"] == total:
                    return
                state["bytes"] = 0
                raise DownloadError("HTTP 416 antes de o arquivo local estar completo")
            if r.status not in (200, 206):
                raise DownloadError(f"HTTP {r.status} ao baixar o áudio")

            mode = "wb"
            if state["bytes"] and r.status == 206:
                m = self._hdr(r.headers, "content-range")
                start = m.split()[-1].split("-")[0] if m.lower().startswith("bytes ") else ""
                if start.isdigit() and int(start) == state["bytes"]:
                    mode = "ab"
                else:
                    state["bytes"] = 0  # Content-Range incompatível: recomeça
            else:
                state["bytes"] = 0  # servidor ignorou o Range (200): recomeça do zero

            length = int(self._hdr(r.headers, "content-length") or 0)
            total = state["bytes"] + length if length else 0
            state["total"] = total
            if self._parallel and not state.get("announced"):
                size = f" [{total / (1024 * 1024):.1f} MB]" if total else ""
                ui.step(f"Em Progresso: {name}{size}")
                state["announced"] = True

            with progress.track_bar(total, "  ⬇️", enabled=bar_on, initial=state["bytes"]) as bar:
                with open(tmp, mode) as out:
                    async for chunk in r.iter_chunks(CHUNK_SIZE):
                        if progress.abort_event.is_set():
                            raise KeyboardInterrupt
                        out.write(chunk)
                        state["bytes"] += len(chunk)
                        bar.update(len(chunk))
        if total and state["bytes"] < total:
            raise DownloadError("download incompleto")

    async def _fetch_segment(self, url: str) -> bytes:
        buf = bytearray()
        async with self.api.http.stream(url) as r:
            if r.status in (401, 403, 404, 451):
                raise PermanentDownloadError(f"HTTP {r.status} em segmento DASH")
            if r.status >= 400:
                raise DownloadError(f"HTTP {r.status} em segmento DASH")
            async for chunk in r.iter_chunks(CHUNK_SIZE):
                buf += chunk
        if not buf:
            raise DownloadError("segmento DASH vazio")
        return bytes(buf)

    async def _dash_attempt(self, urls: list, tmp: str, state: dict, name: str, bar_on: bool) -> None:
        """Baixa os segmentos que faltam; cada segmento só é gravado inteiro (resume limpo)."""
        done = state["seg"]
        if self._parallel and not state.get("announced"):
            ui.step(f"Em Progresso: {name} [{len(urls)} segmentos]")
            state["announced"] = True
        with progress.track_bar(len(urls), "  ↪️", unit="seg", enabled=bar_on, initial=done) as bar:
            with open(tmp, "ab" if done else "wb") as out:
                for i in range(done, len(urls)):
                    if progress.abort_event.is_set():
                        raise KeyboardInterrupt
                    data = await self._fetch_segment(urls[i])
                    out.write(data)
                    state["seg"] = i + 1
                    state["bytes"] += len(data)
                    bar.update(1)
                    bar.set_postfix_str(human_size(state["bytes"]))

    async def _download_stream(self, stream: Stream, tmp: str, name: str) -> int:
        """Baixa o stream para ``tmp`` com retry/backoff e retomada entre tentativas."""
        attempts = self.settings.retries
        bar_on = self.settings.progress_bar and not self._parallel
        state: dict = {"bytes": 0, "seg": 0}
        if not self._parallel:
            ui.step(f"Em Progresso: {name}")

        def warn_retry(n: int, exc: BaseException) -> None:
            ui.warn(f"Falha de Rede. Tentativa {n + 1}/{attempts} para {name} ({exc})")

        async def once() -> None:
            if progress.abort_event.is_set():
                raise KeyboardInterrupt
            if stream.is_dash:
                await self._dash_attempt(stream.urls, tmp, state, name, bar_on)
            else:
                await self._bts_attempt(stream.urls[0], tmp, state, name, bar_on)

        await retry_async(
            once,
            attempts=attempts,
            base_delay=2.0,
            max_delay=32.0,
            retry_on=(NetworkError, DownloadError, OSError),
            give_up_on=(asyncio.CancelledError, KeyboardInterrupt, PermanentDownloadError),
            sleep=self._sleep,
            on_retry=warn_retry,
        )
        if state["bytes"] == 0:
            raise DownloadError("download vazio")
        return state["bytes"]

    # ------------------------------------------------------------------
    # Remux
    # ------------------------------------------------------------------

    async def _ffmpeg_remux(self, src: str, dst: str) -> bool:
        exe = encontrar_binario("ffmpeg")
        if not exe:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "-loglevel", "error", "-y", "-i", src, "-c:a", "copy", "-vn", "-f", "flac", dst,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
        except (OSError, NotImplementedError) as exc:  # a-Shell pode não permitir subprocess
            logger.debug("ffmpeg indisponível: %s", exc)
            return False
        if proc.returncode != 0:
            logger.debug("ffmpeg falhou: %s", err.decode("utf-8", "replace")[:200])
            remove_quiet(dst)
            return False
        return True

    async def _remux_flac(self, src: str, dst: str) -> dict:
        """fMP4/FLAC -> FLAC nativo conforme ``settings.remux``. Devolve info do STREAMINFO."""
        mode = self.settings.remux
        if mode in ("auto", "python"):
            try:
                return await asyncio.to_thread(fmp4.remux_fmp4_flac, src, dst)
            except DownloadError as exc:
                if mode == "python":
                    raise
                logger.debug("remux Python falhou (%s); tentando ffmpeg", exc)
        if mode in ("auto", "ffmpeg") and await self._ffmpeg_remux(src, dst):
            return {}
        raise DownloadError(
            "não foi possível converter o stream para FLAC (remux Python e ffmpeg falharam)"
        )

    # ------------------------------------------------------------------
    # Capa e letras
    # ------------------------------------------------------------------

    async def _cover(self, album: Album) -> Optional[bytes]:
        if not album.cover or not (self.settings.embed_art or self.settings.save_cover_file):
            return None
        if album.cover not in self._cover_cache:
            url = cover_url(album.cover, self.settings.cover_size)
            self._cover_cache[album.cover] = await self._fetch_bytes(url) if url else None
        return self._cover_cache[album.cover]

    async def _album_for(self, track: Track) -> Album:
        """Álbum completo de uma faixa (com cache) -- usado em faixa avulsa/playlist."""
        if track.album_id in self._album_cache:
            return self._album_cache[track.album_id]
        album = await self.api.get_album(track.album_id)
        self._album_cache[track.album_id] = album
        return album

    # ------------------------------------------------------------------
    # Nomes
    # ------------------------------------------------------------------

    def album_folder(self, album: Album) -> str:
        fmt, depth, rate = quality_fields(album.audio_quality, self.settings.quality)
        values = {
            "release_type": album.release_type,
            "album_artist": album.album_artist,
            "album_title": album.full_title,
            "year": album.year,
            "format": fmt,
            "bit_depth": depth or "",
            "sampling_rate": rate,
            "album_id": album.id,
            "quality": QUALITY_MAP[self.settings.quality],
        }
        fallback = f"{album.album_artist} - {album.full_title}"
        rel = render_path(self.settings.folder_format, values, fallback)
        return os.path.join(self.settings.directory, rel)

    def track_basename(self, track: Track, album: Album, *, multi_disc: bool) -> str:
        values = {
            "track_number": f"{track.track_number:02d}",
            "disc_number": f"{track.volume_number:02d}",
            "track_title": sanitize_component(track.full_title),
            "track_title_base": sanitize_component(track.title),
            "track_artist": sanitize_component(track.artist_names),
            "album_artist": sanitize_component(album.album_artist),
            "explicit": "(Explicit)" if track.explicit else "",
            "track_id": track.id,
        }
        tpl = self.settings.multiple_disc_track_format if multi_disc else self.settings.track_format
        fallback = f"{values['track_number']}. {values['track_title']}"
        name = render_template(tpl, values, fallback)
        return sanitize_component(" ".join(name.split()))

    # ------------------------------------------------------------------
    # Uma faixa
    # ------------------------------------------------------------------

    def _existing(self, folder: str, base: str) -> Optional[str]:
        for ext in ("flac", "m4a", "mp4"):
            p = os.path.join(folder, f"{base}.{ext}")
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
        return None

    async def _download_one(
        self,
        track: Track,
        album: Album,
        folder: str,
        base: str,
        *,
        cover: Optional[bytes],
        total_tracks: int,
        label: str,
    ) -> TrackResult:
        res = TrackResult(track.id, track.full_title)
        os.makedirs(folder, exist_ok=True)

        found = self._existing(folder, base)
        if found:
            ui.skip(f"{label}: já existe")
            res.success = res.skipped = True
            res.path = found
            return res

        tmp = os.path.join(folder, f"{TMP_PREFIX}{track.id}.part")
        remux_tmp = os.path.join(folder, f"{TMP_PREFIX}{track.id}.flac")
        try:
            if not track.available:
                raise NonStreamable("faixa indisponível na sua região/conta")

            def on_fallback(frm: str, to: str, why: Exception) -> None:
                ui.warn(f"{label}: {frm} indisponível, tentando {to}")

            stream = await resolve_stream(
                self.api, track.id, self.settings.quality,
                allow_fallback=self.settings.allow_quality_fallback, on_fallback=on_fallback,
            )
            size = await self._download_stream(stream, tmp, label)

            bit_depth, rate = stream.bit_depth, stream.sample_rate
            audio_path = tmp
            ext = stream.extension
            if stream.is_dash and stream.is_flac:
                if self.settings.remux == "none":
                    ext = "mp4"
                else:
                    if not self._parallel:
                        ui.step(" └─ Montando o arquivo FLAC final...")
                    info = await self._remux_flac(tmp, remux_tmp)
                    remove_quiet(tmp)
                    audio_path = remux_tmp
                    bit_depth = info.get("bit_depth") or bit_depth
                    rate = info.get("sample_rate") or rate
            elif stream.is_dash:
                ext = "m4a"

            lyrics_plain, lrc = "", None
            if self.settings.lyrics:
                data = await self.api.get_lyrics(track.id)
                if not data.get("lyrics") and not data.get("subtitles") and self.settings.lyrics_fallback:
                    data = await lyr.fetch_lrclib_lyrics(
                        self.api.http, track.artist_names, track.title, album.full_title
                    )
                lyrics_plain, lrc = lyr.plain_lyrics(data), lyr.synced_lyrics(data)

            final = os.path.join(folder, truncate_name(base, OK_MAX_CHARACTER_LENGTH, f".{ext}") + f".{ext}")
            if ext in ("flac", "m4a"):
                tags = metadata.build_tags(
                    track, album, stream, total_tracks=total_tracks,
                    total_discs=album.number_of_volumes, lyrics=lyrics_plain,
                )
                try:
                    await asyncio.to_thread(
                        metadata.tag_file, audio_path, tags, cover if self.settings.embed_art else None
                    )
                except ImportError:
                    if not self._warned_tags:
                        ui.warn("mutagen não instalado: arquivos ficarão SEM tags (pip install mutagen)")
                        self._warned_tags = True
                except Exception as exc:
                    ui.warn(f"{label}: falha ao gravar tags ({exc}); arquivo mantido")

            os.replace(audio_path, final)
            if lrc and self.settings.save_lrc:
                try:
                    with open(os.path.splitext(final)[0] + ".lrc", "w", encoding="utf-8") as fh:
                        fh.write(lrc)
                except OSError:
                    pass

            res.success = True
            res.path = final
            res.quality = stream.quality
            res.file_format = "FLAC" if ext == "flac" else "AAC"
            res.bit_depth, res.sample_rate = bit_depth, rate
            ui.ok(f" └─ Concluído: {label} ({human_size(size)}, {res.file_format} {stream.quality})")
        except FATAL:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            res.error = str(exc) or type(exc).__name__
            ui.error(f"{label}: {res.error}")
            logger.debug("falha em %s", track.id, exc_info=True)
        finally:
            remove_quiet(tmp)
            remove_quiet(remux_tmp)
        if self.settings.delay:
            await self._sleep(self.settings.delay)
        return res

    # ------------------------------------------------------------------
    # Pastas com estado
    # ------------------------------------------------------------------

    @staticmethod
    def _variants(final_dir: str) -> tuple[str, str]:
        parent, leaf = os.path.split(final_dir.rstrip("/\\"))
        return (os.path.join(parent, MARK_IN_PROGRESS + leaf), os.path.join(parent, MARK_INCOMPLETE + leaf))

    def _prepare_workdir(self, final_dir: str) -> str:
        """Devolve a pasta ``[IN PROGRESS]``, reaproveitando uma anterior."""
        progress, incomplete = self._variants(final_dir)
        for old in (progress, incomplete, final_dir):
            if os.path.isdir(old):
                if old != progress:
                    os.replace(old, progress)
                return progress
        os.makedirs(progress, exist_ok=True)
        return progress

    def _finish_workdir(self, work: str, final_dir: str, ok: bool) -> str:
        _progress, incomplete = self._variants(final_dir)
        target = final_dir if ok else incomplete
        if os.path.isdir(target) and target != work:
            shutil.rmtree(target, ignore_errors=True)
        os.replace(work, target)
        return target

    # ------------------------------------------------------------------
    # Álbum
    # ------------------------------------------------------------------

    def _print_summary(self, kind: str, title: str, result: "AlbumResult") -> None:
        """Bloco final (igual ao RESUMO DO ÁLBUM do qobuz-dl-ultra)."""
        total = len(result.tracks)
        skipped = sum(1 for t in result.tracks if t.skipped)
        failed = result.failed
        downloaded = sum(1 for t in result.tracks if t.success and not t.skipped)
        lowered = sum(1 for t in result.tracks if t.success and not t.skipped
                      and QUALITY_BY_NAME.get(t.quality, 9) < self.settings.quality)
        ui.emit(f"\n{CYAN}{'-' * 44}{RESET}")
        ui.emit(f"  📊 RESUMO DA {kind}: {title}")
        ui.emit(f" - Baixadas com sucesso : {GREEN}{downloaded}/{total}{RESET}")
        if skipped:
            ui.emit(f" - Já existiam (puladas) : {YELLOW}{skipped}{RESET}")
        if lowered:
            ui.emit(f" - Em qualidade menor (fallback) : {YELLOW}{lowered}{RESET}")
        if failed:
            ui.emit(f" - Falhas : {RED}{failed}{RESET}")
        ui.emit(f"{CYAN}{'-' * 44}{RESET}\n")

    async def download_album(self, album_id: Any) -> AlbumResult:
        album, tracks = await self.api.get_album_with_tracks(album_id)
        self._album_cache[album.id] = album
        result = AlbumResult(album.id, album.full_title, album.album_artist)
        final_dir = self.album_folder(album)
        result.folder = final_dir

        if self.db_path:
            prev = await dbm.a_is_downloaded(self.db_path, album.id, "album", self.settings.quality)
            if prev is not None:
                ui.skip(f"{album.album_artist} - {album.full_title}: já baixado ({prev or 'sem caminho'})")
                result.skipped = True
                return result

        multi = album.number_of_volumes > 1
        fmt, depth, rate = quality_fields(album.audio_quality, self.settings.quality)
        mode = self._configure_mode(len(tracks))
        ui.header("ÁLBUM", [
            ("Álbum", album.full_title), ("Artista", album.album_artist),
            ("Ano", album.year), ("Faixas", str(len(tracks) or album.number_of_tracks)),
            ("Qualidade", f"{fmt} {depth}bit" if depth else fmt), ("Modo", mode),
        ])
        if not tracks:
            ui.warn("Álbum sem faixas disponíveis.")
            return result

        work = await asyncio.to_thread(self._prepare_workdir, final_dir)
        cover = await self._cover(album)
        if cover and self.settings.save_cover_file:
            with open(os.path.join(work, "cover.jpg"), "wb") as fh:
                fh.write(cover)

        total = len(tracks)
        jobs = []
        for i, t in enumerate(tracks, start=1):
            base = self.track_basename(t, album, multi_disc=multi)
            sub = os.path.join(work, f"CD {t.volume_number:02d}") if multi else work
            jobs.append(self._download_one(
                t, album, sub, base, cover=cover, total_tracks=total,
                label=f"[{i}/{total}] {t.full_title}",
            ))
        try:
            with progress.sigint_guard():
                result.tracks = await run_limited(jobs, self._workers)
        except BaseException:
            # CTRL+C/cancelamento/erro fatal: deixa a pasta marcada como incompleta
            await asyncio.to_thread(self._finish_workdir, work, final_dir, False)
            raise

        ok = result.failed == 0
        final = await asyncio.to_thread(self._finish_workdir, work, final_dir, ok)
        result.folder = final
        for t in result.tracks:  # os caminhos apontavam para a pasta [IN PROGRESS]
            if t.path.startswith(work):
                t.path = final + t.path[len(work):]
        self._print_summary("ÁLBUM", album.full_title, result)
        if ok:
            await self._record_album(album, result, final)
        else:
            ui.warn(f"{result.failed} faixa(s) falharam: pasta marcada como [INCOMPLETE]. "
                    "Rode o mesmo comando de novo para retomar.")
        return result

    async def _record_album(self, album: Album, result: AlbumResult, final: str) -> None:
        ranks = [QUALITY_BY_NAME.get(t.quality, self.settings.quality) for t in result.tracks if t.quality]
        quality = min(ranks) if ranks else self.settings.quality
        first = next((t for t in result.tracks if t.file_format), None)
        if self.settings.write_sentinel:
            payload = sentinel.build_payload(
                "tidal", album.id, album.full_title, album.album_artist,
                album.number_of_tracks or len(result.tracks),
                [{"id": str(t.track_id), "title": t.title, "success": t.success,
                  "path": os.path.basename(t.path) if t.path else None} for t in result.tracks],
                release_date=album.release_date,
                quality={"tier": QUALITY_MAP.get(quality), "format": first.file_format if first else "",
                         "bit_depth": first.bit_depth if first else None},
            )
            await asyncio.to_thread(sentinel.write_sentinel, final, payload)
        if self.db_path:
            await dbm.a_mark_downloaded(
                self.db_path, album.id, "album", quality=quality, saved_path=final,
                file_format=first.file_format if first else "",
                bit_depth=first.bit_depth if first else None,
                artist=album.album_artist, album=album.full_title, release_date=album.release_date,
            )

    # ------------------------------------------------------------------
    # Faixa avulsa
    # ------------------------------------------------------------------

    async def download_track(self, track_id: Any) -> AlbumResult:
        track = await self.api.get_track(track_id)
        album = await self._album_for(track)
        result = AlbumResult(track.album_id, album.full_title, album.album_artist)
        if self.db_path:
            prev = await dbm.a_is_downloaded(self.db_path, track.id, "track", self.settings.quality)
            if prev:
                ui.skip(f"{track.artist_names} - {track.full_title}: já baixada")
                result.skipped = True
                return result
        folder = self.album_folder(album)
        result.folder = folder
        multi = album.number_of_volumes > 1
        mode = self._configure_mode(1)
        ui.header("FAIXA", [
            ("Faixa", track.full_title), ("Artista", track.artist_names),
            ("Álbum", album.full_title), ("Modo", mode),
        ])
        sub = os.path.join(folder, f"CD {track.volume_number:02d}") if multi else folder
        base = self.track_basename(track, album, multi_disc=multi)
        cover = await self._cover(album)
        with progress.sigint_guard():
            tr = await self._download_one(
                track, album, sub, base, cover=cover, total_tracks=album.number_of_tracks,
                label=f"{track.artist_names} - {track.full_title}",
            )
        result.tracks = [tr]
        if tr.success and not tr.skipped and self.db_path:
            await dbm.a_mark_downloaded(
                self.db_path, track.id, "track",
                quality=QUALITY_BY_NAME.get(tr.quality, self.settings.quality),
                saved_path=tr.path, file_format=tr.file_format, bit_depth=tr.bit_depth,
                artist=track.artist_names, album=album.full_title, title=track.full_title,
                release_date=album.release_date,
            )
        return result

    # ------------------------------------------------------------------
    # Playlist
    # ------------------------------------------------------------------

    async def download_playlist(self, uuid: str) -> AlbumResult:
        pl = await self.api.get_playlist(uuid)
        tracks = await self.api.get_playlist_tracks(uuid)
        result = AlbumResult(uuid, pl.title, pl.creator)
        rel = render_path(
            self.settings.playlist_folder,
            {"playlist_title": pl.title, "playlist_id": uuid, "creator": pl.creator}, pl.title,
        )
        folder = os.path.join(self.settings.directory, rel)
        result.folder = folder
        mode = self._configure_mode(len(tracks))
        ui.header("PLAYLIST", [("Playlist", pl.title), ("Faixas", str(len(tracks))), ("Modo", mode)])
        os.makedirs(folder, exist_ok=True)

        total = len(tracks)
        width = max(2, len(str(total)))

        async def one(i: int, t: Track) -> TrackResult:
            album = await self._album_for(t)
            base = sanitize_component(f"{i:0{width}d}. {t.artist_names} - {t.full_title}")
            cover = await self._cover(album)
            return await self._download_one(
                t, album, folder, base, cover=cover, total_tracks=album.number_of_tracks,
                label=f"[{i}/{total}] {t.artist_names} - {t.full_title}",
            )

        with progress.sigint_guard():
            result.tracks = await run_limited(
                [one(i, t) for i, t in enumerate(tracks, start=1)], self._workers
            )
        self._print_summary("PLAYLIST", pl.title, result)
        ok_paths = [os.path.relpath(t.path, folder) for t in result.tracks if t.success and t.path]
        if ok_paths:
            with open(os.path.join(folder, sanitize_component(pl.title) + ".m3u8"), "w", encoding="utf-8") as fh:
                fh.write("#EXTM3U\n" + "\n".join(ok_paths) + "\n")
        return result

    # ------------------------------------------------------------------
    # Vídeo (HLS)
    # ------------------------------------------------------------------

    async def _ffmpeg_remux_generic(self, src: str, dst: str, *, extra: list[str] = ()) -> bool:
        """Remux genérico ``-c copy`` (reaproveitado pelo vídeo: .ts -> .mp4)."""
        exe = encontrar_binario("ffmpeg")
        if not exe:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "-loglevel", "error", "-y", "-i", src, "-c", "copy", *extra, dst,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
        except (OSError, NotImplementedError) as exc:
            logger.debug("ffmpeg indisponível para remux de vídeo: %s", exc)
            return False
        if proc.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
            logger.debug("ffmpeg (vídeo) falhou: %s", err.decode("utf-8", "replace")[:200])
            remove_quiet(dst)
            return False
        return True

    def video_filename(self, video: Video) -> str:
        year = f" ({video.year})" if video.release_date else ""
        return sanitize_component(f"{video.artist_names} - {video.full_title}{year}")

    async def download_video(self, video_id: Any) -> AlbumResult:
        """Baixa um vídeo musical do Tidal (HLS: ``.ts`` cru, ou ``.mp4`` com ffmpeg/fMP4).

        Sem resumo entre execuções (ao contrário do áudio): uma interrupção faz
        a próxima tentativa recomeçar o vídeo do zero. Cada segmento individual
        tem retry. Segmentos criptografados (AES-128, padrão do HLS) exigem o
        pacote opcional ``cryptography``.
        """
        try:
            video = await self.api.get_video(video_id)
        except ResourceNotFoundError:
            raise
        result = AlbumResult(video.id, video.full_title, video.artist_names)
        video_dir = self.settings.video_directory
        os.makedirs(video_dir, exist_ok=True)
        result.folder = video_dir

        mode = self._configure_mode(1)
        ui.header("VÍDEO", [
            ("Vídeo", video.full_title), ("Artista", video.artist_names),
            ("Ano", video.year or "?"), ("Qualidade", self.settings.video_quality), ("Modo", mode),
        ])

        base = self.video_filename(video)
        if self.db_path:
            saved = await dbm.a_is_downloaded(self.db_path, video.id, "video")
            if saved and os.path.isfile(saved):
                ui.skip(f"{video.artist_names} - {video.full_title}: já existe")
                result.skipped = True
                result.tracks = [TrackResult(video.id, video.full_title, success=True, skipped=True, path=saved)]
                return result

        tr = TrackResult(video.id, video.full_title)
        tmp = os.path.join(video_dir, f"{TMP_PREFIX}{video.id}.video")
        label = f"{video.artist_names} - {video.full_title}"
        try:
            if not video.available:
                raise NonStreamable("vídeo indisponível na sua região/conta")
            top_url = await resolve_video_playback(self.api, video.id, self.settings.video_quality)

            ui.step(f"Em Progresso: {label}")

            def warn_retry(n: int, exc: BaseException) -> None:
                ui.warn(f"Falha de Rede. Tentativa {n + 1}/{self.settings.retries} para {label} ({exc})")

            with progress.track_bar(0, "  ↪️", unit="seg", enabled=self.settings.progress_bar) as bar:
                stats = await hls.download_hls(
                    self.api.http, top_url, tmp,
                    quality=self.settings.video_quality, retries=self.settings.retries,
                    sleep=self._sleep, bar=bar, on_retry=warn_retry,
                    abort_check=progress.abort_event.is_set,
                )

            ext = "mp4" if stats["fragmented"] else "ts"
            final = os.path.join(video_dir, f"{base}.{ext}")
            if not stats["fragmented"] and self.settings.remux != "none":
                remuxed = os.path.join(video_dir, f"{TMP_PREFIX}remux_{video.id}.mp4")
                if await self._ffmpeg_remux_generic(tmp, remuxed):
                    os.replace(remuxed, os.path.join(video_dir, f"{base}.mp4"))
                    remove_quiet(tmp)
                    final = os.path.join(video_dir, f"{base}.mp4")
                else:
                    os.replace(tmp, final)
            else:
                os.replace(tmp, final)

            tr.success = True
            tr.path = final
            tr.quality = self.settings.video_quality
            tr.file_format = "MP4" if final.endswith(".mp4") else "TS"
            ui.ok(f" └─ Concluído: {label} ({human_size(stats['bytes'])}, {tr.file_format})")
            if self.db_path:
                await dbm.a_mark_downloaded(
                    self.db_path, video.id, "video", quality=0, saved_path=final,
                    file_format=tr.file_format, artist=video.artist_names, title=video.full_title,
                    release_date=video.release_date,
                )
        except FATAL:
            remove_quiet(tmp)
            raise
        except asyncio.CancelledError:
            remove_quiet(tmp)
            raise
        except Exception as exc:
            tr.error = str(exc) or type(exc).__name__
            ui.error(f"{label}: {tr.error}")
            logger.debug("falha ao baixar vídeo %s", video.id, exc_info=True)
            remove_quiet(tmp)
        result.tracks = [tr]
        self._print_summary("VÍDEO", video.full_title, result)
        return result

