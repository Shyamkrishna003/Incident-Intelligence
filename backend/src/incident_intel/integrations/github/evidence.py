"""Code changes as investigation evidence: commit messages and file names from GitHub.

File contents are never fetched or passed on. Commit messages and paths are written by
developers and are untrusted text: they are redacted and truncated like log messages.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from incident_intel.core.crypto import SecretBox, SecretDecryptionError
from incident_intel.core.logging import scrub_text
from incident_intel.incidents.queries import CandidateDeployment
from incident_intel.integrations.github.client import CodeChange, GitHubClient, GitHubError
from incident_intel.integrations.github.models import GitHubConnection, ServiceRepository
from incident_intel.investigation.code_changes import CodeChangeEvidence
from incident_intel.telemetry.models import Deployment
from incident_intel.tenancy.context import TenantScope

logger = structlog.get_logger(__name__)

# Each deployment costs one GitHub request and adds to the prompt.
MAX_DEPLOYMENTS = 3
MAX_MESSAGE = 200
MAX_PATH = 200


@dataclass(frozen=True)
class _Previous:
    version: str
    commit_sha: str


@dataclass(frozen=True)
class _Lookup:
    token_encrypted: str
    repositories: dict[str, str]
    previous: dict[uuid.UUID, _Previous]


def _clean(text: str | None, limit: int) -> str | None:
    return scrub_text(text)[:limit] if text else None


def code_change_data(
    candidate: CandidateDeployment,
    repository: str,
    previous: _Previous | None,
    change: CodeChange,
    *,
    rollback: bool = False,
) -> dict[str, Any]:
    deployment = candidate.deployment
    data: dict[str, Any] = {
        "service": candidate.service_name,
        "version": deployment.version,
        "repository": repository,
        "commit": deployment.commit_sha,
        "compared_with": (
            {"version": previous.version, "commit": previous.commit_sha}
            if previous
            else "No earlier deployment of this service with a commit is known; this is "
            "the deployed commit alone, which may be only part of what changed."
        ),
        "commits": [
            {
                "sha": commit.sha,
                "message": _clean(commit.message, MAX_MESSAGE),
                "author": _clean(commit.author, 100),
                "date": commit.authored_at,
            }
            for commit in change.commits
        ],
        "total_commits": change.total_commits,
        "files": [
            {
                "path": _clean(file.path, MAX_PATH),
                "status": file.status,
                "lines_added": file.additions,
                "lines_removed": file.deletions,
            }
            for file in change.files
        ],
        "total_files": change.total_files,
        "note": "Commit messages and file names from the repository. File contents are "
        "not included. A change being part of a deployment does not show that it caused "
        "the incident.",
    }
    if rollback:
        data["rollback"] = (
            "The deployed commit is older than the previous one: this deployment moved the "
            "service back to earlier code. The commits and files listed are what it removed."
        )
    elif change.relationship == "diverged":
        data["diverged"] = (
            "The two commits are on different lines of history; the lists show what is "
            "new in the deployed commit."
        )
    return data


class GitHubCodeChanges:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        box: SecretBox,
        client: GitHubClient,
    ) -> None:
        self._session_factory = session_factory
        self._box = box
        self._client = client

    async def _lookup(
        self, scope: TenantScope, deployments: Sequence[CandidateDeployment]
    ) -> _Lookup | None:
        async with self._session_factory() as session:
            connection = await session.scalar(
                select(GitHubConnection).where(
                    GitHubConnection.organization_id == scope.organization_id,
                    GitHubConnection.project_id == scope.project_id,
                )
            )
            if connection is None:
                return None
            rows = await session.execute(
                select(ServiceRepository.service_name, ServiceRepository.repository).where(
                    ServiceRepository.project_id == scope.project_id,
                    ServiceRepository.service_name.in_(
                        sorted({item.service_name for item in deployments})
                    ),
                )
            )
            repositories = {name: repository for name, repository in rows}
            previous: dict[uuid.UUID, _Previous] = {}
            for item in deployments:
                if item.service_name not in repositories:
                    continue
                earlier = (
                    await session.execute(
                        select(Deployment.version, Deployment.commit_sha)
                        .where(
                            Deployment.project_id == scope.project_id,
                            Deployment.service_id == item.deployment.service_id,
                            Deployment.deployed_at < item.deployment.deployed_at,
                            Deployment.commit_sha.is_not(None),
                        )
                        .order_by(Deployment.deployed_at.desc())
                        .limit(1)
                    )
                ).one_or_none()
                if earlier is not None and earlier.commit_sha:
                    previous[item.deployment.id] = _Previous(earlier.version, earlier.commit_sha)
            return _Lookup(connection.token_encrypted, repositories, previous)

    async def collect(
        self, scope: TenantScope, deployments: Sequence[CandidateDeployment]
    ) -> CodeChangeEvidence:
        # Closest to the incident first: those are the ones worth the request budget.
        with_commit = [item for item in reversed(deployments) if item.deployment.commit_sha]
        if not with_commit:
            return CodeChangeEvidence(summary={"skipped": "no candidate deployment has a commit"})
        with_commit = with_commit[:MAX_DEPLOYMENTS]

        lookup = await self._lookup(scope, with_commit)
        if lookup is None:
            return CodeChangeEvidence(summary={"skipped": "GitHub is not connected"})
        try:
            token = self._box.decrypt(lookup.token_encrypted)
        except SecretDecryptionError:
            logger.warning("github_token_undecryptable", project_id=str(scope.project_id))
            return CodeChangeEvidence(
                notes=[
                    "Code changes were not included: the stored GitHub token could not be "
                    "read. Connect GitHub again in the project's settings."
                ],
                summary={"skipped": "stored token could not be decrypted"},
            )

        items: list[tuple[str, dict[str, Any]]] = []
        notes: list[str] = []
        unmapped = failed = 0
        for candidate in with_commit:
            deployment = candidate.deployment
            label = f"{candidate.service_name} {deployment.version}"
            repository = lookup.repositories.get(candidate.service_name)
            if repository is None:
                unmapped += 1
                notes.append(
                    f"Code changes for {label} were not included: no repository is set for "
                    f"service {candidate.service_name}."
                )
                continue
            previous = lookup.previous.get(deployment.id)
            head = deployment.commit_sha or ""
            rollback = False
            try:
                if previous is not None and previous.commit_sha != head:
                    change = await self._client.compare(
                        token, repository, previous.commit_sha, head
                    )
                    if change.relationship == "behind":
                        # A rollback adds nothing; what matters is what it took away.
                        rollback = True
                        change = await self._client.compare(
                            token, repository, head, previous.commit_sha
                        )
                else:
                    previous = None
                    change = await self._client.commit(token, repository, head)
            except GitHubError as exc:
                failed += 1
                logger.info("github_code_change_failed", repository=repository, kind=exc.kind)
                notes.append(f"Code changes for {label} could not be fetched from GitHub: {exc}.")
                continue
            items.append(
                (
                    f"Code changes in deployment: {label}",
                    code_change_data(candidate, repository, previous, change, rollback=rollback),
                )
            )
        return CodeChangeEvidence(
            items=items,
            notes=notes,
            summary={
                "deployments": len(with_commit),
                "fetched": len(items),
                "failed": failed,
                "unmapped": unmapped,
            },
        )

    async def close(self) -> None:
        await self._client.close()
