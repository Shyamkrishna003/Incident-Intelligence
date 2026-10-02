from enum import StrEnum


class Role(StrEnum):
    """A user's role in an organization. Each role includes everything below it."""

    VIEWER = "viewer"  # read projects, services, and metrics
    MEMBER = "member"  # same as viewer for now; will act on incidents later
    ADMIN = "admin"  # also create projects and manage API keys
    OWNER = "owner"  # everything; the user who created the organization

    def at_least(self, minimum: "Role") -> bool:
        return _RANK[self] >= _RANK[minimum]


_RANK = {Role.VIEWER: 0, Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}
