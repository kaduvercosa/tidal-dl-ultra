"""Testes da camada HTTP (retry, rate limit, mapeamento de erros)."""

import asyncio

import pytest

from fakes import FakeBackend, jresp, make_client
from tidal_dl import exceptions as ex
from tidal_dl import net
from tidal_dl.net import HttpClient, NetworkError, RateLimiter, Response, raise_for_status


def run(c):
    return asyncio.run(c)


def test_response_json_invalido_vira_vazio():
    assert Response(200, {}, b"nao json").json() == {}
    assert Response(200, {"Retry-After": "3"}, b"").header("retry-after") == "3"


@pytest.mark.parametrize("status,exc", [
    (401, ex.AuthenticationError), (403, ex.ForbiddenError), (404, ex.ResourceNotFoundError),
    (429, ex.RateLimitError), (500, ex.ApiError),
])
def test_raise_for_status(status, exc):
    with pytest.raises(exc):
        raise_for_status(jresp({"userMessage": "x"}, status))
    raise_for_status(Response(204))


def test_rate_limit_carrega_retry_after():
    with pytest.raises(ex.RateLimitError) as e:
        raise_for_status(Response(429, {"Retry-After": "12"}, b"{}"))
    assert e.value.retry_after == 12.0


def test_retry_em_5xx_depois_sucesso():
    seq = [jresp({}, 503), jresp({}, 500), jresp({"ok": 1})]
    client, backend = make_client(lambda *a: seq.pop(0))
    r = run(client.request("GET", "http://x"))
    assert r.status == 200 and len(backend.calls) == 3


def test_retry_em_429_respeita_retry_after():
    waited = []
    seq = [Response(429, {"Retry-After": "5"}, b"{}"), jresp({"ok": 1})]

    async def sleep(s):
        waited.append(s)

    backend = FakeBackend(lambda *a: seq.pop(0))
    client = HttpClient(backend, requests_per_minute=0, sleep=sleep, base_delay=1.0)
    assert run(client.request("GET", "http://x")).status == 200
    assert waited == [5.0]


def test_5xx_esgotado_devolve_ultima_resposta():
    client, _ = make_client(lambda *a: jresp({}, 500), retries=2)
    assert run(client.request("GET", "http://x")).status == 500


def test_erro_de_rede_com_retry_e_esgotamento():
    n = []

    def handler(*a):
        n.append(1)
        raise NetworkError("boom")

    client, _ = make_client(handler, retries=3)
    with pytest.raises(NetworkError):
        run(client.request("GET", "http://x"))
    assert len(n) == 3


def test_retry_desligado():
    client, backend = make_client(lambda *a: jresp({}, 500))
    run(client.request("GET", "http://x", retry=False))
    assert len(backend.calls) == 1


def test_4xx_nao_repete():
    client, backend = make_client(lambda *a: jresp({}, 404))
    assert run(client.request("GET", "http://x")).status == 404
    assert len(backend.calls) == 1


def test_rate_limiter_espera_intervalo():
    now = [0.0]
    waited = []

    async def sleep(s):
        waited.append(s)
        now[0] += s

    rl = RateLimiter(60, clock=lambda: now[0], sleep=sleep)  # 1 req/s

    async def go():
        await rl.wait()
        await rl.wait()
        await rl.wait()

    run(go())
    assert waited == [1.0, 1.0]
    assert RateLimiter(0)._interval == 0.0
