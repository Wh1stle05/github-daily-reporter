"""Small, defensive transport for GitHub's REST and GraphQL APIs."""

import asyncio
import base64
from datetime import UTC, datetime
import math
import random
import re
import time
from typing import Any, Awaitable, Callable, Mapping

import httpx
from pydantic import ValidationError

from github_daily_reporter.models import RepositoryCandidate


API_URL = "https://api.github.com"
DEFAULT_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "github-daily-reporter/0.1",
}
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_README_ENCODED_CHARS = 1_000_000
OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class GitHubRequestError(RuntimeError):
    """A safe-to-display failure while communicating with GitHub."""


class GitHubClient:
    """Authenticated GitHub API client, valid only inside an async context."""

    def __init__(
        self,
        token: str,
        timeout: float = 20,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._token = token
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None
        # Callers with a collection-wide deadline may set this monotonic time.
        # It intentionally defaults to None: request timeout is not a collection deadline.
        self.collection_deadline: float | None = None

    async def __aenter__(self) -> "GitHubClient":
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=API_URL,
                timeout=self._timeout,
                headers={**DEFAULT_HEADERS, "Authorization": f"Bearer {self._token}"},
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def rest_json(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._request("GET", path, params=params)
        return self._json(response, "GitHub API response was not valid JSON")

    async def graphql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "POST", "/graphql", json={"query": query, "variables": dict(variables)}
        )
        payload = self._json(response, "GitHub GraphQL response was not valid JSON")
        if payload.get("errors"):
            raise GitHubRequestError("GitHub GraphQL request failed")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GitHubRequestError("GitHub GraphQL response was incomplete")
        return data

    async def get_repository(
        self, canonical_name: str, signals: Mapping[str, Any]
    ) -> RepositoryCandidate:
        canonical_name = self._validate_canonical_name(canonical_name)
        data = await self.rest_json(f"/repos/{canonical_name}")
        full_name = self._text(data.get("full_name")) or canonical_name
        license_data = data.get("license")
        license_spdx = (
            self._text(license_data.get("spdx_id"))
            if isinstance(license_data, dict)
            else None
        )
        values: dict[str, Any] = {
            "canonical_name": full_name.lower(),
            "full_name": full_name,
            "html_url": self._text(data.get("html_url")) or "",
            "description": self._nullable_text(data.get("description")),
            "created_at": self._parse_timestamp(data.get("created_at")),
            "pushed_at": self._parse_timestamp(data.get("pushed_at"), required=False),
            "archived": bool(data.get("archived", False)),
            "disabled": bool(data.get("disabled", False)),
            "is_fork": bool(data.get("fork", False)),
            "is_empty": data.get("size") == 0,
            "license_spdx": license_spdx,
            "primary_language": self._nullable_text(data.get("language")),
            "stars_total": self._nonnegative_int(data.get("stargazers_count")),
            "forks_total": self._nonnegative_int(data.get("forks_count")),
            "open_issues_count": self._nonnegative_int(data.get("open_issues_count")),
        }
        self._merge_valid_signals(values, signals)
        try:
            return RepositoryCandidate.model_validate(values)
        except ValidationError as exc:
            # GitHub supplied unusable public metadata.  Keep its details private.
            raise GitHubRequestError("GitHub repository response was incomplete") from None

    async def get_readme_excerpt(self, canonical_name: str, max_chars: int = 2000) -> str:
        canonical_name = self._validate_canonical_name(canonical_name)
        if max_chars <= 0:
            return ""
        response = await self._request(
            "GET", f"/repos/{canonical_name}/readme", allowed_statuses={404}
        )
        if response.status_code == 404:
            return ""
        self._raise_for_status(response)
        payload = self._json(response, "GitHub README response was not valid JSON")
        content = payload.get("content")
        if not isinstance(content, str):
            return ""
        if len(content) > self._readme_encoded_limit(max_chars):
            raise GitHubRequestError("GitHub README response was too large")
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
        except (ValueError, TypeError):
            raise GitHubRequestError("GitHub README response was invalid") from None
        return decoded.decode("utf-8", errors="replace")[: max(max_chars, 0)]

    @staticmethod
    def _readme_encoded_limit(max_chars: int) -> int:
        bounded_chars = min(max_chars, MAX_README_ENCODED_CHARS)
        decoded_bytes = bounded_chars * 4
        encoded_chars = 4 * ((decoded_bytes + 2) // 3)
        line_break_allowance = encoded_chars // 60 + 8
        return min(encoded_chars + line_break_allowance, MAX_README_ENCODED_CHARS)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        allowed_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        client = self._require_client()
        relative_path = "/" + path.lstrip("/")
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await client.request(method, relative_path, **kwargs)
            except httpx.TransportError:
                if attempt == self._max_attempts:
                    raise GitHubRequestError(
                        "GitHub API request could not be completed"
                    ) from None
                await self._wait_before_retry(None, attempt)
                continue
            except Exception:
                raise GitHubRequestError("GitHub API request could not be completed") from None

            if not self._is_retryable(response):
                if response.status_code >= 400 and response.status_code not in (
                    allowed_statuses or set()
                ):
                    self._raise_for_status(response)
                return response
            if attempt == self._max_attempts:
                self._raise_for_status(response)

            await self._wait_before_retry(response, attempt)

        raise GitHubRequestError("GitHub API request failed")

    async def _wait_before_retry(
        self, response: httpx.Response | None, attempt: int
    ) -> None:
        delay = self._retry_delay(response, attempt)
        if (
            self.collection_deadline is not None
            and time.monotonic() + delay > self.collection_deadline
        ):
            raise GitHubRequestError("GitHub retry wait exceeds the collection deadline")
        await self._sleep(delay)

    @staticmethod
    def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
        if response is not None:
            retry_after = GitHubClient._valid_retry_after(response)
            if retry_after is not None:
                return min(retry_after, 60.0)

        if response is not None and response.headers.get("X-RateLimit-Remaining") == "0":
            reset = response.headers.get("X-RateLimit-Reset")
            if reset is not None:
                try:
                    delay = float(reset) - time.time()
                    if math.isfinite(delay):
                        return min(max(delay, 0.0), 60.0)
                except ValueError:
                    pass

        return min(2 ** (attempt - 1) + random.uniform(0, 0.25), 10.0)

    @staticmethod
    def _is_retryable(response: httpx.Response) -> bool:
        if response.status_code in RETRYABLE_STATUS:
            return True
        return response.status_code == 403 and (
            response.headers.get("X-RateLimit-Remaining") == "0"
            or GitHubClient._valid_retry_after(response) is not None
        )

    @staticmethod
    def _valid_retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            delay = float(value)
        except ValueError:
            return None
        return delay if math.isfinite(delay) and delay >= 0 else None

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("GitHubClient must be used as an async context manager")
        return self._client

    @staticmethod
    def _validate_canonical_name(canonical_name: str) -> str:
        if not isinstance(canonical_name, str) or canonical_name.count("/") != 1:
            raise GitHubRequestError("GitHub repository name was invalid")
        owner, repository = canonical_name.split("/", 1)
        if (
            OWNER_PATTERN.fullmatch(owner) is None
            or REPOSITORY_PATTERN.fullmatch(repository) is None
            or repository in {".", ".."}
        ):
            raise GitHubRequestError("GitHub repository name was invalid")
        return canonical_name

    @staticmethod
    def _json(response: httpx.Response, message: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise GitHubRequestError(message) from None
        if not isinstance(payload, dict):
            raise GitHubRequestError(message)
        return payload

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise GitHubRequestError(
                f"GitHub API request failed (HTTP {response.status_code})"
            )

    @staticmethod
    def _text(value: Any) -> str | None:
        return value if isinstance(value, str) else None

    @classmethod
    def _nullable_text(cls, value: Any) -> str | None:
        return cls._text(value)

    @staticmethod
    def _nonnegative_int(value: Any) -> int:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return 0

    @staticmethod
    def _parse_timestamp(value: Any, required: bool = True) -> datetime | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise GitHubRequestError("GitHub repository response was incomplete")
        try:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone required")
            return parsed.astimezone(UTC)
        except ValueError:
            raise GitHubRequestError("GitHub repository response was incomplete") from None

    @staticmethod
    def _merge_valid_signals(values: dict[str, Any], signals: Mapping[str, Any]) -> None:
        protected = {
            "canonical_name",
            "full_name",
            "html_url",
            "description",
            "created_at",
            "pushed_at",
            "archived",
            "disabled",
            "is_fork",
            "is_empty",
            "license_spdx",
            "primary_language",
            "stars_total",
            "forks_total",
            "open_issues_count",
        }
        for field, value in signals.items():
            if field not in RepositoryCandidate.model_fields or field in protected:
                continue
            try:
                RepositoryCandidate.model_validate({**values, field: value})
            except ValidationError:
                continue
            values[field] = value
