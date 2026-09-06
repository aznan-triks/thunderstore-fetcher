"""Tests for the async Thunderstore HTTP client (retries, logging, pagination)."""
import asyncio
import logging

import httpx
import pytest

import thunderstore as ts


def run(coro):
    return asyncio.run(coro)


def _client(handler, concurrency=2, delay=0.0):
    return ts.ThunderstoreClient(
        concurrency=concurrency, delay=delay, transport=httpx.MockTransport(handler)
    )


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    # Keep retry back-off negligible during tests.
    monkeypatch.setattr(ts, "_RETRY_BASE", 0.01)


# ── _get: retries & logging ─────────────────────────────────────────────────────


def test_get_retries_on_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, json={"detail": "unavailable"})
        return httpx.Response(200, json={"results": [{"name": "ModA"}], "next": None})

    async def scenario():
        async with _client(handler) as client:
            packages = await client.list_packages("test-community")
            assert [p["name"] for p in packages] == ["ModA"]

    run(scenario())
    assert calls["n"] == 3  # 2 failures + 1 success


def test_get_raises_and_logs_after_exhausting_retries(caplog):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"detail": "boom"})

    async def scenario():
        async with _client(handler) as client:
            await client.list_packages("test-community")

    with caplog.at_level(logging.ERROR, logger="thunderstore"):
        with pytest.raises(httpx.HTTPStatusError):
            run(scenario())
    assert calls["n"] == ts._MAX_RETRIES
    assert any("failed" in r.message for r in caplog.records)


def test_get_does_not_retry_4xx():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"detail": "nope"})

    async def scenario():
        async with _client(handler) as client:
            await client.list_packages("unknown-community")

    with pytest.raises(httpx.HTTPStatusError):
        run(scenario())
    assert calls["n"] == 1  # 404 is not retryable


def test_get_retries_on_timeout_then_raises_and_logs(caplog):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    async def scenario():
        async with _client(handler) as client:
            await client.list_packages("test-community")

    with caplog.at_level(logging.ERROR, logger="thunderstore"):
        with pytest.raises(httpx.ReadTimeout):
            run(scenario())
    assert calls["n"] == ts._MAX_RETRIES
    assert any("failed after" in r.message for r in caplog.records)


# ── pagination ──────────────────────────────────────────────────────────────────


def test_list_packages_follows_next_pages():
    urls = []

    def handler(request):
        urls.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"results": [{"name": "ModB"}], "next": None})
        return httpx.Response(
            200, json={"results": [{"name": "ModA"}], "next": f"{ts.BASE_URL}/c/x/api/v1/package/?page=2"}
        )

    async def scenario():
        async with _client(handler) as client:
            packages = await client.list_packages("x")
            assert [p["name"] for p in packages] == ["ModA", "ModB"]

    run(scenario())
    assert len(urls) == 2


def test_list_packages_accepts_plain_array_response():
    def handler(request):
        return httpx.Response(200, json=[{"name": "OnlyMod"}])

    async def scenario():
        async with _client(handler) as client:
            packages = await client.list_packages("x")
            assert [p["name"] for p in packages] == ["OnlyMod"]

    run(scenario())


def test_list_communities_paginates_and_stops_on_no_next():
    pages = iter(
        [
            {"results": [{"name": "Risk of Rain 2", "identifier": "riskofrain2"}],
             "next": "https://thunderstore.io/api/cyberstorm/community/?cursor=x"},
            {"results": [{"name": "Valheim", "identifier": "valheim"}], "next": None},
        ]
    )

    def handler(request):
        return httpx.Response(200, json=next(pages))

    async def scenario():
        async with _client(handler) as client:
            communities = await client.list_communities()
            assert len(communities) == 2

    run(scenario())


def test_list_communities_total_failure_raises(caplog):
    def handler(request):
        return httpx.Response(500, json={})

    async def scenario():
        async with _client(handler) as client:
            await client.list_communities()

    with caplog.at_level(logging.WARNING, logger="thunderstore"):
        with pytest.raises(httpx.HTTPStatusError):
            run(scenario())
    assert any("Failed to list communities" in r.message for r in caplog.records)


def test_list_communities_partial_failure_returns_collected(caplog):
    page1 = {"results": [{"name": "Risk of Rain 2", "identifier": "riskofrain2"}],
             "next": "https://thunderstore.io/api/cyberstorm/community/?cursor=x"}
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(500, json={})

    async def scenario():
        async with _client(handler) as client:
            communities = await client.list_communities()
            assert len(communities) == 1  # kept what was collected

    with caplog.at_level(logging.WARNING, logger="thunderstore"):
        run(scenario())
    assert any("Failed to list communities" in r.message for r in caplog.records)


# ── categories: soft fallback vs. raise_on_error ────────────────────────────────


def test_get_categories_soft_fallback_on_error(caplog):
    def handler(request):
        return httpx.Response(500, json={})

    async def scenario():
        async with _client(handler) as client:
            categories = await client.get_categories("x")
            assert categories == []

    with caplog.at_level(logging.WARNING, logger="thunderstore"):
        run(scenario())
    assert any("Failed to fetch categories" in r.message for r in caplog.records)


def test_get_categories_raise_on_error(caplog):
    def handler(request):
        return httpx.Response(500, json={})

    async def scenario():
        async with _client(handler) as client:
            await client.get_categories("x", raise_on_error=True)

    with caplog.at_level(logging.WARNING, logger="thunderstore"):
        with pytest.raises(httpx.HTTPStatusError):
            run(scenario())


def test_concurrency_semaphore_limits_parallel_requests():
    """Requests must be serialized by concurrency (worker count)."""
    active = {"now": 0, "max": 0}
    import threading

    lock = threading.Lock()

    def handler(request):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            return httpx.Response(200, json=[{"name": "M"}])
        finally:
            with lock:
                active["now"] -= 1

    async def scenario():
        async with _client(handler, concurrency=2) as client:
            await asyncio.gather(*[client.list_packages("x") for _ in range(6)])

    run(scenario())
    assert active["max"] <= 2
