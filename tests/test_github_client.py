import base64
from datetime import UTC, datetime
import math

import httpx
import pytest
import respx

from github_daily_reporter.github_client import GitHubClient, GitHubRequestError


@pytest.mark.asyncio
@respx.mock
async def test_retries_503_then_returns_json():
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    route = respx.get("https://api.github.com/repos/owner/repo").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"full_name": "Owner/Repo"}),
        ]
    )

    async with GitHubClient("secret", max_attempts=2, sleep=no_sleep) as client:
        data = await client.rest_json("/repos/owner/repo")

    assert route.call_count == 2
    assert data["full_name"] == "Owner/Repo"
    assert len(delays) == 1


@pytest.mark.asyncio
@respx.mock
async def test_error_never_contains_token():
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(401, text="bad")
    )

    async with GitHubClient("secret-token", max_attempts=1) as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.rest_json("/repos/owner/repo")

    assert "secret-token" not in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_http_error_json_is_not_returned_as_a_payload():
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(401, json={"message": "bad credentials"})
    )

    async with GitHubClient("secret-token", max_attempts=1) as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.rest_json("/repos/owner/repo")

    assert "bad credentials" not in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected_delay"),
    [
        ({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1005"}, 5),
        ({"Retry-After": "2.5"}, 2.5),
    ],
)
@respx.mock
async def test_retries_403_only_when_rate_limited(monkeypatch, headers, expected_delay):
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("github_daily_reporter.github_client.time.time", lambda: 1000)
    route = respx.get("https://api.github.com/repos/owner/repo").mock(
        side_effect=[
            httpx.Response(403, headers=headers),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with GitHubClient("secret", max_attempts=2, sleep=no_sleep) as client:
        assert await client.rest_json("/repos/owner/repo") == {"ok": True}

    assert route.call_count == 2
    assert delays == [expected_delay]


@pytest.mark.asyncio
@respx.mock
async def test_ordinary_403_is_terminal_without_retry():
    route = respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(403, json={"message": "forbidden"})
    )

    async def no_sleep(_seconds: float) -> None:
        raise AssertionError("ordinary 403 must not sleep")

    async with GitHubClient("secret", max_attempts=3, sleep=no_sleep) as client:
        with pytest.raises(GitHubRequestError):
            await client.rest_json("/repos/owner/repo")

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_get_repository_maps_public_metadata_and_valid_signals():
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "Owner/Repo",
                "html_url": "https://github.com/Owner/Repo",
                "description": "tool",
                "created_at": "2026-07-20T00:00:00Z",
                "pushed_at": "2026-07-22T00:00:00Z",
                "archived": False,
                "disabled": False,
                "fork": False,
                "size": 12,
                "license": {"spdx_id": "MIT"},
                "language": "Python",
                "stargazers_count": 80,
                "forks_count": 4,
                "open_issues_count": 2,
            },
        )
    )

    async with GitHubClient("secret") as client:
        candidate = await client.get_repository(
            "owner/repo",
            {
                "discovery_sources": {"trending"},
                "trending_rank": 3,
                "stars_24h": -1,
                "not_a_candidate_field": "ignored",
            },
        )

    assert candidate.canonical_name == "owner/repo"
    assert candidate.license_spdx == "MIT"
    assert candidate.primary_language == "Python"
    assert candidate.created_at == datetime(2026, 7, 20, tzinfo=UTC)
    assert candidate.trending_rank == 3
    assert candidate.stars_24h is None


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", ["2026-07-20T00:00:00", "2026-07-20"])
@respx.mock
async def test_get_repository_rejects_timestamp_without_timezone(timestamp):
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "Owner/Repo",
                "html_url": "https://github.com/Owner/Repo",
                "created_at": timestamp,
            },
        )
    )

    async with GitHubClient("secret") as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.get_repository("owner/repo", {})

    assert timestamp not in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_get_repository_normalizes_offset_timestamp_to_utc():
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(
            200,
            json={
                "full_name": "Owner/Repo",
                "html_url": "https://github.com/Owner/Repo",
                "created_at": "2026-07-20T08:00:00+08:00",
            },
        )
    )

    async with GitHubClient("secret") as client:
        candidate = await client.get_repository("owner/repo", {})

    assert candidate.created_at == datetime(2026, 7, 20, tzinfo=UTC)
    assert candidate.created_at.tzinfo is UTC


@pytest.mark.asyncio
@respx.mock
async def test_graphql_errors_are_sanitized():
    query = "query { viewer { login } } # private-note"
    respx.post("https://api.github.com/graphql").mock(
        return_value=httpx.Response(200, json={"errors": [{"message": "private detail"}]})
    )

    async with GitHubClient("secret") as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.graphql(query, {"secret": "hidden"})

    message = str(exc.value)
    assert "private" not in message
    assert "hidden" not in message


@pytest.mark.asyncio
@respx.mock
async def test_get_readme_excerpt_bounds_decoded_text():
    encoded = base64.b64encode(b"hello\xff world").decode("ascii")
    respx.get("https://api.github.com/repos/owner/repo/readme").mock(
        return_value=httpx.Response(200, json={"content": encoded})
    )

    async with GitHubClient("secret") as client:
        excerpt = await client.get_readme_excerpt("owner/repo", max_chars=7)

    assert excerpt == "hello\ufffd "


@pytest.mark.asyncio
@respx.mock
async def test_get_readme_excerpt_rejects_base64_junk():
    respx.get("https://api.github.com/repos/owner/repo/readme").mock(
        return_value=httpx.Response(200, json={"content": "aGVsbG8=!!!"})
    )

    async with GitHubClient("secret") as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.get_readme_excerpt("owner/repo")

    assert "aGVsbG8" not in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_get_readme_excerpt_returns_empty_for_404():
    respx.get("https://api.github.com/repos/owner/repo/readme").mock(
        return_value=httpx.Response(404)
    )

    async with GitHubClient("secret") as client:
        assert await client.get_readme_excerpt("owner/repo") == ""


@pytest.mark.asyncio
@respx.mock
async def test_nonpositive_readme_limit_returns_without_request_or_decode():
    route = respx.get("https://api.github.com/repos/owner/repo/readme").mock(
        return_value=httpx.Response(200, json={"content": "invalid!"})
    )

    async with GitHubClient("secret") as client:
        assert await client.get_readme_excerpt("owner/repo", max_chars=0) == ""

    assert route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_get_readme_excerpt_rejects_oversized_encoded_content():
    respx.get("https://api.github.com/repos/owner/repo/readme").mock(
        return_value=httpx.Response(200, json={"content": "A" * 100_000})
    )

    async with GitHubClient("secret") as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.get_readme_excerpt("owner/repo", max_chars=1)

    assert "A" * 100 not in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_retry_exhaustion_is_sanitized():
    respx.get("https://api.github.com/repos/owner/repo").mock(
        return_value=httpx.Response(503, text="external failure")
    )

    async def no_sleep(_seconds: float) -> None:
        return None

    async with GitHubClient("secret", max_attempts=2, sleep=no_sleep) as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.rest_json("/repos/owner/repo")

    assert "external failure" not in str(exc.value)
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_retries_transient_transport_error_then_succeeds():
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    request = httpx.Request("GET", "https://api.github.com/repos/owner/repo")
    route = respx.get("https://api.github.com/repos/owner/repo").mock(
        side_effect=[
            httpx.ConnectError("connection failed", request=request),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with GitHubClient("secret", max_attempts=2, sleep=no_sleep) as client:
        assert await client.rest_json("/repos/owner/repo") == {"ok": True}

    assert route.call_count == 2
    assert len(delays) == 1
    assert 1 <= delays[0] <= 1.25


@pytest.mark.asyncio
@respx.mock
async def test_transport_retry_exhaustion_never_leaks_exception_details():
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    secret_url = "https://api.github.com/repos/owner/repo?token=secret-token"
    request = httpx.Request("GET", secret_url)
    route = respx.get("https://api.github.com/repos/owner/repo").mock(
        side_effect=httpx.ReadTimeout(secret_url, request=request)
    )

    async with GitHubClient("secret-token", max_attempts=2, sleep=no_sleep) as client:
        with pytest.raises(GitHubRequestError) as exc:
            await client.rest_json("/repos/owner/repo")

    assert route.call_count == 2
    assert len(delays) == 1
    assert "secret-token" not in str(exc.value)
    assert secret_url not in str(exc.value)


@pytest.mark.asyncio
async def test_use_before_enter_is_rejected_without_creating_client():
    client = GitHubClient("secret")

    with pytest.raises(RuntimeError, match="async context manager"):
        await client.rest_json("/repos/owner/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["get_repository", "get_readme_excerpt"])
@respx.mock
async def test_canonical_name_injection_is_rejected_before_request(method_name):
    route = respx.route().mock(return_value=httpx.Response(404))
    injected = "owner/repo?access_token=private-value"

    async with GitHubClient("secret") as client:
        with pytest.raises(GitHubRequestError) as exc:
            if method_name == "get_repository":
                await client.get_repository(injected, {})
            else:
                await client.get_readme_excerpt(injected)

    assert route.call_count == 0
    assert "private-value" not in str(exc.value)


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_wait_uses_reset_and_never_sleeps_more_than_sixty_seconds(monkeypatch):
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("github_daily_reporter.github_client.time.time", lambda: 1000)
    route = respx.get("https://api.github.com/repos/owner/repo").mock(
        side_effect=[
            httpx.Response(
                429,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2000"},
            ),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with GitHubClient("secret", max_attempts=2, sleep=no_sleep) as client:
        assert await client.rest_json("/repos/owner/repo") == {"ok": True}

    assert route.call_count == 2
    assert delays == [60]


@pytest.mark.asyncio
@respx.mock
async def test_non_finite_retry_after_falls_back_to_a_finite_backoff():
    delays: list[float] = []

    async def no_sleep(seconds: float) -> None:
        delays.append(seconds)

    respx.get("https://api.github.com/repos/owner/repo").mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "NaN"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    async with GitHubClient("secret", max_attempts=2, sleep=no_sleep) as client:
        assert await client.rest_json("/repos/owner/repo") == {"ok": True}

    assert len(delays) == 1
    assert math.isfinite(delays[0])
    assert 1 <= delays[0] <= 1.25
