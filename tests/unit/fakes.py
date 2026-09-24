"""Backend HTTP falso do Tidal para os testes (sem rede, sem httpx)."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import struct
from urllib.parse import parse_qs, urlsplit

from tidal_dl.net import HttpClient, Response


def box(t: bytes, p: bytes) -> bytes:
    return struct.pack(">I", 8 + len(p)) + t + p


def make_init(sample_rate=44100, channels=2, bits=16) -> bytes:
    si = bytearray(34)
    packed = (sample_rate << 44) | ((channels - 1) << 41) | ((bits - 1) << 36) | 1000
    si[10:18] = packed.to_bytes(8, "big")
    blocks = bytes([0x80]) + (34).to_bytes(3, "big") + bytes(si)
    dfla = box(b"dfLa", b"\0\0\0\0" + blocks)
    entry = box(b"fLaC", bytes(28) + dfla)
    stsd = box(b"stsd", b"\0\0\0\0\0\0\0\1" + entry)
    moov = box(b"moov", box(b"trak", box(b"mdia", box(b"minf", box(b"stbl", stsd)))))
    return box(b"ftyp", b"isom\0\0\0\0") + moov


def make_segment(payload: bytes) -> bytes:
    return box(b"moof", b"\0" * 16) + box(b"mdat", payload)


def dash_mpd(base: str, segments: int = 2) -> str:
    return (
        '<?xml version="1.0"?><MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><AdaptationSet>'
        '<Representation codecs="flac" bandwidth="1"><SegmentTemplate '
        f'initialization="{base}/init.mp4" media="{base}/seg$Number$.m4s" startNumber="1">'
        f'<SegmentTimeline><S d="1" r="{segments - 1}"/></SegmentTimeline></SegmentTemplate>'
        "</Representation></AdaptationSet></Period></MPD>"
    )


def b64(s) -> str:
    return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


class FakeStream:
    def __init__(self, status, content, headers=None, cut_after=None):
        self.status, self.headers, self._c, self._cut = status, headers or {}, content, cut_after

    async def iter_chunks(self, size):
        from tidal_dl.net import NetworkError

        if self._cut is not None:  # conexão cai no meio do arquivo
            if self._cut:
                yield self._c[: self._cut]
            raise NetworkError("conexão caiu")
        for i in range(0, len(self._c), size):
            yield self._c[i:i + size]


class FakeBackend:
    """Roteia por (método, URL). ``handler``: (method, url, params, data, headers) -> Response."""

    def __init__(self, handler=None):
        self.calls: list[tuple] = []
        self.handler = handler
        self.files: dict[str, bytes] = {}
        self.cut: dict[str, int] = {}  # url -> bytes entregues antes de a conexão cair (uma vez)
        self.honor_range = True

    async def request(self, method, url, *, params=None, data=None, headers=None, timeout=None):
        self.calls.append((method, url, params, data, headers))
        if url in self.files:
            return Response(200, {}, self.files[url])
        return self.handler(method, url, params or {}, data or {}, headers or {})

    @contextlib.asynccontextmanager
    async def stream(self, url, *, headers=None, timeout=None):
        self.calls.append(("STREAM", url, None, None, headers))
        if url not in self.files:
            yield FakeStream(404, b"")
            return
        data = self.files[url]
        rng = (headers or {}).get("Range", "")
        start, status, hdrs = 0, 200, {}
        if rng.startswith("bytes=") and self.honor_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                yield FakeStream(416, b"", {"content-range": f"bytes */{len(data)}"})
                return
            status = 206
            hdrs["content-range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        hdrs["content-length"] = str(len(data) - start)
        yield FakeStream(status, data[start:], hdrs, cut_after=self.cut.pop(url, None))

    async def aclose(self):
        pass


def jresp(obj, status=200, headers=None) -> Response:
    return Response(status, headers or {}, json.dumps(obj).encode())


def make_client(handler=None, **kw):
    backend = FakeBackend(handler)

    async def nosleep(_):
        return None

    return HttpClient(backend, requests_per_minute=0, sleep=nosleep, base_delay=0.0, **kw), backend
