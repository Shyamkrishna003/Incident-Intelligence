import pytest

from incident_intel.cli import build_parser


def test_bootstrap_requires_org_and_project() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bootstrap", "--org", "acme"])


def test_bootstrap_defaults() -> None:
    args = build_parser().parse_args(["bootstrap", "--org", "acme", "--project", "payments"])

    assert args.key_name == "default"
    assert args.org_name is None


def test_api_key_create_accepts_repeatable_known_scopes() -> None:
    args = build_parser().parse_args(
        [
            "api-keys",
            "create",
            "--org",
            "acme",
            "--project",
            "payments",
            "--name",
            "ci",
            "--scope",
            "ingest:write",
            "--scope",
            "telemetry:read",
        ]
    )

    assert args.scope == ["ingest:write", "telemetry:read"]


def test_api_key_create_rejects_unknown_scope() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "api-keys",
                "create",
                "--org",
                "a",
                "--project",
                "b",
                "--name",
                "n",
                "--scope",
                "admin",
            ]
        )
