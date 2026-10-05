"""The GitHub REST calls this application makes. Read-only, bounded, one error type.

The token is passed per call (each project has its own) and travels only in the
Authorization header.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

REPOSITORY_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$"
_REPOSITORY = re.compile(REPOSITORY_PATTERN)
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")

MAX_COMMITS = 20
MAX_FILES = 40

ErrorKind = Literal["unauthorized", "not_found", "rate_limited", "unavailable", "invalid"]

_MESSAGES: dict[ErrorKind, str] = {
    "unauthorized": "GitHub rejected the token",
    "not_found": "the repository or commit was not found, or the token cannot read it",
    "rate_limited": "GitHub's rate limit was reached",
    "unavailable": "GitHub could not be reached",
    "invalid": "GitHub returned an unexpected response",
}


class GitHubError(Exception):
    """A GitHub call failed. The message is safe to show: it never contains the token."""

    def __init__(self, kind: ErrorKind) -> None:
        super().__init__(_MESSAGES[kind])
        self.kind = kind

    @property
    def transient(self) -> bool:
        return self.kind in ("rate_limited", "unavailable")


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    # First line of the commit message only.
    message: str
    author: str | None
    authored_at: str | None


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    additions: int
    deletions: int


@dataclass(frozen=True)
class CodeChange:
    """What changed, capped at MAX_COMMITS and MAX_FILES. Totals count everything GitHub
    reported, so a reader can tell when the lists are incomplete."""

    commits: list[CommitInfo]
    files: list[ChangedFile]
    total_commits: int
    total_files: int
    # "ahead" (the normal case), "behind" (a rollback), "diverged" or "identical";
    # None for a single commit.
    relationship: str | None = None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _commit(raw: Any) -> CommitInfo | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("sha"), str):
        return None
    detail = _mapping(raw.get("commit"))
    author = _mapping(detail.get("author"))
    message = _text(detail.get("message")) or ""
    return CommitInfo(
        sha=raw["sha"][:12],
        message=message.splitlines()[0] if message else "",
        author=_text(author.get("name")),
        authored_at=_text(author.get("date")),
    )


def _file(raw: Any) -> ChangedFile | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("filename"), str):
        return None
    return ChangedFile(
        path=raw["filename"],
        status=_text(raw.get("status")) or "modified",
        additions=_count(raw.get("additions")),
        deletions=_count(raw.get("deletions")),
    )


def _commits(raw: Any) -> list[CommitInfo]:
    """Every parseable commit, oldest first (GitHub's order)."""
    items = raw if isinstance(raw, list) else []
    return [commit for commit in (_commit(entry) for entry in items) if commit is not None]


def _files(raw: Any) -> list[ChangedFile]:
    items = raw if isinstance(raw, list) else []
    return [file for file in (_file(entry) for entry in items) if file is not None]


class GitHubClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Redirects are not followed: the token must never be sent to another host.
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=False
        )

    async def _get(self, token: str, path: str) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "incident-intelligence",
        }
        try:
            response = await self._http.get(f"{self._base_url}{path}", headers=headers)
        except httpx.TransportError as exc:
            raise GitHubError("unavailable") from exc

        status = response.status_code
        if status == 401:
            raise GitHubError("unauthorized")
        if status == 429 or (
            status == 403 and response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise GitHubError("rate_limited")
        if status in (403, 404, 422):
            # GitHub answers 404 for a private repository the token cannot read, and 422
            # for a commit that does not exist in it.
            raise GitHubError("not_found")
        if status >= 500:
            raise GitHubError("unavailable")
        if status != 200:
            raise GitHubError("invalid")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise GitHubError("invalid") from exc
        if not isinstance(payload, dict):
            raise GitHubError("invalid")
        return payload

    @staticmethod
    def _check(repository: str, *commits: str) -> None:
        # Both end up in the request path: only accept the exact shapes we expect.
        if not _REPOSITORY.match(repository) or not all(_COMMIT.match(c) for c in commits):
            raise GitHubError("not_found")

    async def check_repository(self, token: str, repository: str) -> None:
        """Raise GitHubError unless ``token`` can read ``repository``."""
        self._check(repository)
        await self._get(token, f"/repos/{repository}")

    async def compare(self, token: str, repository: str, base: str, head: str) -> CodeChange:
        """What changed from commit ``base`` to commit ``head``."""
        self._check(repository, base, head)
        payload = await self._get(token, f"/repos/{repository}/compare/{base}...{head}")
        commits = _commits(payload.get("commits"))
        files = _files(payload.get("files"))
        return CodeChange(
            # Newest first, so the cap keeps the commits closest to the deployment.
            commits=list(reversed(commits))[:MAX_COMMITS],
            files=files[:MAX_FILES],
            total_commits=max(_count(payload.get("total_commits")), len(commits)),
            total_files=len(files),
            relationship=_text(payload.get("status")),
        )

    async def commit(self, token: str, repository: str, sha: str) -> CodeChange:
        """What one commit changed (used when no earlier deployment is known)."""
        self._check(repository, sha)
        payload = await self._get(token, f"/repos/{repository}/commits/{sha}")
        commit = _commit(payload)
        files = _files(payload.get("files"))
        return CodeChange(
            commits=[commit] if commit else [],
            files=files[:MAX_FILES],
            total_commits=1 if commit else 0,
            total_files=len(files),
        )

    async def close(self) -> None:
        await self._http.aclose()
