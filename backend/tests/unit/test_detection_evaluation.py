"""The evaluation suite itself, and the standing result the default detector must keep."""

from incident_intel.detection.engine import EngineConfig
from incident_intel.detection.evaluation import (
    Report,
    candidates,
    evaluate,
    format_report,
    scenarios,
)
from tests.support import make_settings

DB = "postgresql+asyncpg://user:pw@127.0.0.1:1/unreachable_test"


def _reports() -> dict[str, Report]:
    settings = make_settings(DB)
    factories = candidates(settings.detection_threshold, settings.detection_min_history)
    return {name: evaluate(name, factory, EngineConfig()) for name, factory in factories.items()}


def test_scenarios_are_deterministic_and_labelled() -> None:
    first, second = scenarios(), scenarios()

    assert first == second
    by_name = {scenario.name: scenario for scenario in first}
    incident = by_name["payment_incident"]
    assert sum(bool(series.problems) for series in incident.series) == 7
    assert sum(not series.problems for series in incident.series) == 2
    assert all(not series.problems for series in by_name["healthy_day"].series)


def test_default_detector_keeps_its_evaluated_performance() -> None:
    """A regression guard: changing the detector or its defaults must not silently make
    detection worse on the labelled scenarios. Update these numbers only deliberately."""
    report = evaluate(
        "robust_zscore",
        candidates(make_settings(DB).detection_threshold, 30)["robust_zscore"],
        EngineConfig(),
    )
    by_scenario = {score.scenario: score for score in report.scenarios}

    assert report.false_positives == 0
    assert (by_scenario["payment_incident"].detected, by_scenario["payment_incident"].problems) == (
        7,
        7,
    )
    assert by_scenario["healthy_day"].anomalies == 0
    assert (report.detected, report.problems) == (10, 11)
    # The known miss: a slow drift with no sudden change.
    assert by_scenario["gradual_drift"].missed == ["gradual_drift"]
    assert report.median_seconds_to_detect <= 60


def test_default_detector_beats_the_simple_rule_and_ewma_on_recall() -> None:
    reports = _reports()
    robust = reports["robust_zscore"]
    simple = reports["static_threshold (2x usual)"]
    ewma = reports["ewma"]

    assert robust.detected > simple.detected
    assert robust.detected > ewma.detected
    # The simple rule cannot see a 60% shift or a drop: neither doubles the value.
    missed = {name for score in simple.scenarios for name in score.missed}
    assert missed == {"level_shift", "drop"}


def test_report_is_readable() -> None:
    text = format_report(list(_reports().values()))

    assert "robust_zscore" in text
    assert "10/11" in text
    assert "missed: gradual_drift" in text
