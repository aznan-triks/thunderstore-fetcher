import asyncio
import logging
import httpx
from typing import Optional

logger = logging.getLogger("thunderstore")

BASE_URL = "https://thunderstore.io"
API_V1_PATH = "/api/v1"
CYBERSTORM_PATH = "/api/cyberstorm"

_MAX_RETRIES = 3
_RETRY_BASE = 1.5  # secondes, backoff exponentiel


class ThunderstoreClient:
    def __init__(self, concurrency: int = 4, delay: float = 0.2):
        self.concurrency = concurrency
        self.delay = delay
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=limits,
            headers={
                "Accept": "application/json",
                "User-Agent": "ThunderstoreFetcher/1.0",
            },
        )
        return self

    async def __aexit__(self, *args):
        await self._client.aclose()

    async def _get(self, url: str) -> dict:
        """GET with exponential retry on network errors and 5xx/429."""
        async with self._semaphore:
            await asyncio.sleep(self.delay)
            last_exc: Exception = RuntimeError("No attempts made")
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await self._client.get(url)
                    resp.raise_for_status()
                    return resp.json()
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(_RETRY_BASE ** attempt)
                        last_exc = exc
                    else:
                        raise
                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    await asyncio.sleep(_RETRY_BASE ** attempt)
                    last_exc = exc
            raise last_exc

    # ------------------------------------------------------------------
    # Communities (automatic pagination)
    # ------------------------------------------------------------------
    async def list_communities(self) -> list[dict]:
        results: list[dict] = []
        url: str | None = f"{BASE_URL}{CYBERSTORM_PATH}/community/"
        while url:
            try:
                data = await self._get(url)
                results.extend(data.get("results", []))
                url = data.get("next")
            except Exception as exc:
                # Error logged (never silent); returns what was collected so far.
                logger.warning("Failed to list communities (%s): %s", url, exc)
                break
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
    async def get_categories(self, community: str) -> list[dict]:
        try:
            data = await self._get(
                f"{BASE_URL}/api/experimental/community/{community}/category/"
            )
            return data.get("results", [])
        except Exception as exc:
            # Fall back to an empty list, but the error is logged (CONTEXT.md rule).
            logger.warning("Failed to fetch categories for %s: %s", community, exc)
            return []
