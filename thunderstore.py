import asyncio
import logging
import httpx
from typing import Optional

logger = logging.getLogger("thunderstore")

BASE_URL = "https://thunderstore.io"
API_V1_PATH = "/api/v1"
CYBERSTORM_PATH = "/api/cyberstorm"

_MAX_RETRIES = 3
_RETRY_BASE = 1.5  # seconds, exponential backoff
_RETRYABLE_STATUS = (429, 500, 502, 503, 504)


class ThunderstoreClient:
    def __init__(
        self,
        concurrency: int = 4,
        delay: float = 0.2,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        self.concurrency = concurrency
        self.delay = delay
        self._semaphore = asyncio.Semaphore(concurrency)
        # Optional injected transport — used by the test suite to simulate the
        # Thunderstore API without any network access.
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        kwargs = dict(
            timeout=30,
            limits=limits,
            headers={
                "Accept": "application/json",
                "User-Agent": "ThunderstoreFetcher/1.0",
            },
        )
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)
        return self

    async def __aexit__(self, *args):
        if self._client is not None:
            await self._client.aclose()

    async def _get(self, url: str) -> dict:
        """GET with exponential retry on network errors and 5xx/429.

        Every failure is logged (rule: network errors are never silent).
        After the last retry the underlying exception is re-raised so the
        caller can decide how to surface the failure.
        """
        async with self._semaphore:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await self._client.get(url)
                    resp.raise_for_status()
                    return resp.json()
                except httpx.HTTPStatusError as exc:
                    retryable = exc.response.status_code in _RETRYABLE_STATUS
                    if retryable and attempt < _MAX_RETRIES - 1:
                        logger.warning(
                            "GET %s -> HTTP %s (attempt %d/%d), retrying…",
                            url, exc.response.status_code, attempt + 1, _MAX_RETRIES,
                        )
                        await asyncio.sleep(_RETRY_BASE ** attempt)
                    else:
                        logger.error(
                            "GET %s failed (HTTP %s): %s",
                            url, exc.response.status_code, exc,
                        )
                        raise
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if attempt < _MAX_RETRIES - 1:
                        logger.warning(
                            "GET %s failed (attempt %d/%d): %s — retrying…",
                            url, attempt + 1, _MAX_RETRIES, exc,
                        )
                        await asyncio.sleep(_RETRY_BASE ** attempt)
                    else:
                        logger.error(
                            "GET %s failed after %d attempts: %s",
                            url, _MAX_RETRIES, exc,
                        )
                        raise
            # Unreachable: either a request succeeded or an exception was raised.
            raise RuntimeError("GET %s failed" % url)  # pragma: no cover

    # ------------------------------------------------------------------
    # Communities (automatic pagination)
    # ------------------------------------------------------------------
    async def list_communities(self) -> list[dict]:
        results: list[dict] = []
        url: str | None = f"{BASE_URL}{CYBERSTORM_PATH}/community/"
        first_error: Optional[Exception] = None
        while url:
            try:
                data = await self._get(url)
                results.extend(data.get("results", []))
                url = data.get("next")
            except Exception as exc:
                # Logged (never silent), then keep what was collected so far.
                # A total failure (nothing collected) is re-raised so callers
                # can distinguish "empty API" from "API unreachable".
                logger.warning(
                    "Failed to list communities at %s after %d result(s): %s",
                    url, len(results), exc,
                )
                first_error = first_error or exc
                break
        if first_error and not results:
            raise first_error
        return results

    # ------------------------------------------------------------------
    # Packages for a community (automatic pagination)
    # ------------------------------------------------------------------
    async def list_packages(self, community: str) -> list[dict]:
        packages: list[dict] = []
        url: str | None = f"{BASE_URL}/c/{community}{API_V1_PATH}/package/"
        while url:
            data = await self._get(url)
            if isinstance(data, list):
                packages.extend(data)
                break
            packages.extend(data.get("results", []))
            url = data.get("next")
        return packages

    # ------------------------------------------------------------------
    # Categories for a community
    # ------------------------------------------------------------------
    async def get_categories(
        self, community: str, raise_on_error: bool = False
    ) -> list[dict]:
        try:
            data = await self._get(
                f"{BASE_URL}/api/experimental/community/{community}/category/"
            )
            return data.get("results", [])
        except Exception as exc:
            # Logged (rule). The pipeline degrades gracefully to an empty list
            # (falling back to the categories seen on the mods), while the web
            # UI endpoints opt into raising so they can surface a 502 instead
            # of silently showing "no categories".
            logger.warning("Failed to fetch categories for %s: %s", community, exc)
            if raise_on_error:
                raise
            return []
