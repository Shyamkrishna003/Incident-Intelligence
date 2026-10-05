"""The GitHub client against a stand-in HTTP server."""

from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from incident_intel.core.config import RuntimeSettings, Settings
from incident_intel.core.crypto import generate_key
from incident_intel.core.logging import REDACTED, scrub_text
from incident_intel.integrations.github.client import (
    MAX_COMMITS,
    MAX_FILES,
    GitHubClient,
    GitHubError,
)
from incident_intel.integrations.github.evidence import GitHubCodeChanges
from incident_intel.investigation.llm import WorkerSettings
from incident_intel.investigation.worker import build_code_changes

TOKEN = "github_pat_" + "a1B2" * 10
REPO = "acme/payment-api"


def client(handler: Any) -> GitHubClient:
    return GitHubClient(
        base_url="https://github.test",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def commit(sha: str, message: str, author: str = "Dev") -> dict[str, Any]:
    return {
        "sha": sha,
        "commit": {
            "message": message,
            "author": {"name": author, "date": "2026-10-03T11:00:00Z"},
        },
    }


def changed(path: str, additions: int = 1, deletions: int = 0) -> dict[str, Any]:
    return {
        "filename": path,
        "status": "modified",
        "additions": additions,
        "deletions": deletions,
        "patch": "@@ secret code that must never be passed on @@",
    }


def comparison(commits: list[Any], files: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "status": "ahead",
        "total_commits": len(commits),
        "commits": commits,
        "files": files,
    } | extra


async def test_sends_the_token_in_a_header_only() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=comparison([], []))

    await client(handler).compare(TOKEN, REPO, "aaaaaaa", "bbbbbbb")

    [request] = seen
    assert (
        str(request.url) == "https://github.test/repos/acme/payment-api/compare/aaaaaaa...bbbbbbb"
    )
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert TOKEN not in str(request.url)


async def test_compare_returns_messages_and_file_names_but_never_file_contents() -> None:
    payload = comparison(
        [
            commit("1" * 40, "Add customer reference lookup\n\nLong body that is dropped."),
            commit("2" * 40, "Fix typo"),
        ],
        [changed("payments/queries.py", 30, 2), changed("README.md")],
    )

    change = await client(lambda _: httpx.Response(200, json=payload)).compare(
        TOKEN, REPO, "aaaaaaa", "bbbbbbb"
    )

    # Newest first; first line of each message only; shortened sha.
    assert [(c.sha, c.message) for c in change.commits] == [
        ("2" * 12, "Fix typo"),
        ("1" * 12, "Add customer reference lookup"),
    ]
    assert [(f.path, f.additions, f.deletions) for f in change.files] == [
        ("payments/queries.py", 30, 2),
        ("README.md", 1, 0),
    ]
    assert (change.total_commits, change.total_files, change.relationship) == (2, 2, "ahead")
    assert "secret code" not in repr(change)


async def test_large_changes_are_capped_keeping_the_newest_commits_and_the_totals() -> None:
    payload = comparison(
        [commit(f"{i:040x}", f"commit {i}") for i in range(60)],
        [changed(f"src/file_{i}.py") for i in range(90)],
        total_commits=300,
    )

    change = await client(lambda _: httpx.Response(200, json=payload)).compare(
        TOKEN, REPO, "aaaaaaa", "bbbbbbb"
    )

    assert len(change.commits) == MAX_COMMITS
    assert change.commits[0].message == "commit 59"
    assert len(change.files) == MAX_FILES
    assert (change.total_commits, change.total_files) == (300, 90)


async def test_a_single_commit() -> None:
    payload = {**commit("3" * 40, "Switch to synchronous fraud check"), "files": [changed("a.py")]}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=payload)

    change = await client(handler).commit(TOKEN, REPO, "3333333")

    assert seen == ["/repos/acme/payment-api/commits/3333333"]
    assert [c.message for c in change.commits] == ["Switch to synchronous fraud check"]
    assert (change.total_commits, change.total_files, change.relationship) == (1, 1, None)


async def test_malformed_entries_are_skipped_not_trusted() -> None:
    payload = comparison(
        [{"sha": 5}, "nonsense", commit("4" * 40, "ok")],
        [
            {"filename": None},
            changed("ok.py", additions=-3),
            {"filename": "b.py", "additions": "9"},
        ],
    )

    change = await client(lambda _: httpx.Response(200, json=payload)).compare(
        TOKEN, REPO, "aaaaaaa", "bbbbbbb"
    )

    assert [c.message for c in change.commits] == ["ok"]
    assert [(f.path, f.additions) for f in change.files] == [("ok.py", 0), ("b.py", 0)]


@pytest.mark.parametrize(
    ("response", "kind", "transient"),
    [
        (httpx.Response(401, json={"message": "Bad credentials"}), "unauthorized", False),
        (httpx.Response(404, json={}), "not_found", False),
        (httpx.Response(403, json={}), "not_found", False),
        (httpx.Response(422, json={}), "not_found", False),
        (
            httpx.Response(403, json={}, headers={"x-ratelimit-remaining": "0"}),
            "rate_limited",
            True,
        ),
        (httpx.Response(429, json={}), "rate_limited", True),
        (httpx.Response(502, text="bad gateway"), "unavailable", True),
        (httpx.Response(200, text="<html>"), "invalid", False),
        (httpx.Response(200, json=[1, 2]), "invalid", False),
        # A redirect is never followed: the token must not travel to another host.
        (httpx.Response(302, headers={"Location": "https://evil.test/"}), "invalid", False),
    ],
)
async def test_failures_become_one_error_type(
    response: httpx.Response, kind: str, transient: bool
) -> None:
    with pytest.raises(GitHubError) as caught:
        await client(lambda _: response).check_repository(TOKEN, REPO)

    assert (caught.value.kind, caught.value.transient) == (kind, transient)
    assert TOKEN not in str(caught.value)


async def test_network_failure_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(GitHubError) as caught:
        await client(handler).check_repository(TOKEN, REPO)

    assert caught.value.kind == "unavailable"


@pytest.mark.parametrize(
    ("repository", "base", "head"),
    [
        ("acme/../../user", "aaaaaaa", "bbbbbbb"),
        ("acme/repo?x=1", "aaaaaaa", "bbbbbbb"),
        ("no-slash", "aaaaaaa", "bbbbbbb"),
        (REPO, "main", "bbbbbbb"),
        (REPO, "aaaaaaa", "bbbbbbb/../../x"),
    ],
)
async def test_unexpected_repository_or_commit_shapes_never_reach_the_network(
    repository: str, base: str, head: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    with pytest.raises(GitHubError) as caught:
        await client(handler).compare(TOKEN, repository, base, head)

    assert caught.value.kind == "not_found"


def test_github_tokens_are_redacted_from_text() -> None:
    classic = "ghp_" + "Z9y8" * 9
    scrubbed = scrub_text(f"clone failed for https://x:{TOKEN}@github.com and {classic} too")
    assert TOKEN not in scrubbed
    assert classic not in scrubbed
    assert REDACTED in scrubbed


# --- Who holds the encryption key ---------------------------------------------------------


async def test_the_worker_reads_code_changes_only_when_it_has_the_encryption_key() -> None:
    url = "postgresql+asyncpg://u:p@127.0.0.1:1/x_test"
    key = generate_key()
    with_key = WorkerSettings(database_url=url, secrets_encryption_key=key)

    source = build_code_changes(with_key, async_sessionmaker())
    disabled = build_code_changes(
        WorkerSettings(database_url=url, secrets_encryption_key=""), async_sessionmaker()
    )

    assert isinstance(source, GitHubCodeChanges)
    await source.close()
    assert disabled is None
    assert key not in repr(with_key)
    assert key not in with_key.model_dump_json()


def test_consumers_cannot_load_the_encryption_key() -> None:
    assert "secrets_encryption_key" not in RuntimeSettings.model_fields
    assert "secrets_encryption_key" in Settings.model_fields
    assert "secrets_encryption_key" in WorkerSettings.model_fields
