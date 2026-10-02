"""Detectors and the anomaly engine: pure logic, no database."""

from datetime import UTC, datetime, timedelta

import pytest

from incident_intel.detection.detectors import (
    EwmaDetector,
    RobustZScoreDetector,
    StaticThresholdDetector,
    build_detector,
)
from incident_intel.detection.engine import (
    EngineConfig,
    EngineState,
    Episode,
    Point,
    run_engine,
    severity_for,
)

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
STEP = timedelta(seconds=15)
DETECTOR = RobustZScoreDetector(threshold=6.0, min_history=10)
CONFIG = EngineConfig(window=50, min_consecutive=2, close_after=timedelta(seconds=45))

# A healthy series: 100, with a small repeating wobble.
NORMAL = [100.0, 101.0, 99.0, 100.5, 99.5]


def series(*values: float, start: int = 0) -> list[Point]:
    return [Point(T0 + (start + i) * STEP, value) for i, value in enumerate(values)]


def normal(count: int, start: int = 0) -> list[Point]:
    return series(*[NORMAL[i % len(NORMAL)] for i in range(count)], start=start)


def run(points: list[Point], state: EngineState | None = None) -> tuple[list[Episode], EngineState]:
    result = run_engine(DETECTOR, CONFIG, state or EngineState(), points)
    return result.closed, result.state


# --- Detectors ---------------------------------------------------------------------------


def test_robust_zscore_judges_distance_from_the_median() -> None:
    baseline = [NORMAL[i % 5] for i in range(30)]

    normal_value = DETECTOR.evaluate(baseline, 102.0)
    high = DETECTOR.evaluate(baseline, 180.0)
    low = DETECTOR.evaluate(baseline, 20.0)

    assert normal_value is not None
    assert not normal_value.anomalous
    assert high is not None
    assert high.anomalous
    assert high.direction == "above"
    assert low is not None
    assert low.anomalous
    assert low.direction == "below"
    assert high.center == pytest.approx(100.0)


def test_no_judgment_without_enough_history() -> None:
    assert DETECTOR.evaluate([100.0] * 9, 500.0) is None
    assert EwmaDetector(min_history=10).evaluate([100.0] * 9, 500.0) is None


def test_a_flat_series_does_not_make_tiny_changes_anomalous() -> None:
    flat = [100.0] * 30

    small = DETECTOR.evaluate(flat, 101.0)
    large = DETECTOR.evaluate(flat, 150.0)

    assert small is not None
    assert not small.anomalous
    assert large is not None
    assert large.anomalous


def test_a_few_earlier_spikes_do_not_blind_the_robust_detector() -> None:
    baseline = [NORMAL[i % 5] for i in range(30)]
    baseline[5] = baseline[12] = baseline[20] = 900.0

    verdict = DETECTOR.evaluate(baseline, 400.0)

    assert verdict is not None
    assert verdict.anomalous


def test_ewma_follows_recent_values() -> None:
    detector = EwmaDetector(threshold=6.0, min_history=10)
    rising = [100.0 + i for i in range(40)]

    verdict = detector.evaluate(rising, 140.0)

    assert verdict is not None
    assert verdict.center > 125  # weighted toward the recent end
    assert not verdict.anomalous


def test_static_threshold_is_a_fixed_limit() -> None:
    detector = StaticThresholdDetector(upper=200.0)

    assert detector.evaluate([], 200.0) == pytest.approx(detector.evaluate([1.0] * 99, 200.0))
    below, above = detector.evaluate([], 200.0), detector.evaluate([], 201.0)
    assert below is not None
    assert not below.anomalous
    assert above is not None
    assert above.anomalous


def test_build_detector_by_name() -> None:
    assert isinstance(
        build_detector("robust_zscore", threshold=5, min_history=20), RobustZScoreDetector
    )
    assert build_detector("ewma", threshold=5, min_history=20).threshold == 5
    with pytest.raises(ValueError, match="unknown detector"):
        build_detector("magic", threshold=5, min_history=20)


@pytest.mark.parametrize(
    ("score", "expected"),
    [(6.0, "low"), (11.9, "low"), (12.0, "medium"), (24.0, "high"), (-48.0, "critical")],
)
def test_severity_is_a_rule_on_the_score(score: float, expected: str) -> None:
    assert severity_for(score, 6.0) == expected


# --- Engine ------------------------------------------------------------------------------


def test_a_single_odd_point_opens_nothing_and_does_not_poison_the_baseline() -> None:
    closed, state = run([*normal(20), *series(500.0, start=20), *normal(20, start=21)])

    assert closed == []
    assert state.open is None
    assert state.pending_count == 0
    assert len(state.baseline) == 41  # the odd point was kept as ordinary noise


def test_consecutive_anomalous_points_open_one_episode_that_extends() -> None:
    closed, state = run([*normal(20), *series(300.0, 400.0, 500.0, start=20)])

    assert closed == []
    episode = state.open
    assert episode is not None
    assert episode.started_at == T0 + 20 * STEP
    assert episode.detected_at == T0 + 21 * STEP  # opened by the second point
    assert episode.last_anomalous_at == T0 + 22 * STEP
    assert episode.point_count == 3
    assert episode.direction == "above"
    assert (episode.peak_value, episode.peak_at) == (500.0, T0 + 22 * STEP)
    assert episode.baseline_center == pytest.approx(100.0)


def test_episode_closes_only_after_staying_normal_long_enough() -> None:
    points = [*normal(20), *series(300.0, 300.0, start=20)]
    still_open, state = run([*points, *normal(2, start=22)])  # 15 s and 30 s after
    closed, state_after = run(normal(1, start=24), state)  # 45 s after: closes

    assert still_open == []
    assert state.open is not None
    [episode] = closed
    assert episode.closed_reason == "recovered"
    assert episode.ended_at == T0 + 24 * STEP
    assert episode.last_anomalous_at == T0 + 21 * STEP
    assert state_after.open is None


def test_a_brief_return_to_normal_does_not_split_an_episode() -> None:
    closed, state = run(
        [*normal(20), *series(300.0, 300.0, 100.0, 300.0, start=20), *normal(3, start=24)]
    )

    [episode] = closed
    assert episode.point_count == 3
    assert episode.last_anomalous_at == T0 + 23 * STEP
    assert state.open is None


def test_a_sustained_shift_stays_one_episode_because_the_baseline_is_frozen() -> None:
    closed, state = run([*normal(20), *series(*[300.0] * 200, start=20)])

    assert closed == []
    assert state.open is not None
    assert state.open.point_count == 200
    assert all(point.value < 150 for point in state.baseline)  # no shifted point leaked in


def test_an_episode_open_too_long_is_accepted_as_the_new_normal() -> None:
    config = EngineConfig(
        window=50, close_after=timedelta(seconds=45), max_open=timedelta(minutes=5)
    )
    points = [*normal(20), *series(*[300.0] * 60, start=20)]

    result = run_engine(DETECTOR, config, EngineState(), points)

    [episode] = result.closed
    assert episode.closed_reason == "persisted"
    assert episode.ended_at == T0 + 40 * STEP  # five minutes after it started
    assert result.state.open is None  # 300 is now the baseline
    assert {point.value for point in result.state.baseline} == {300.0}


def test_severity_and_peak_grow_with_the_episode() -> None:
    _, early = run([*normal(20), *series(140.0, 140.0, start=20)])
    _, later = run([*normal(20), *series(140.0, 140.0, 900.0, 200.0, start=20)])

    assert early.open is not None
    assert later.open is not None
    assert early.open.severity == "low"
    assert later.open.severity == "critical"
    assert later.open.peak_value == 900.0


def test_a_drop_is_reported_as_below() -> None:
    _, state = run([*normal(20), *series(10.0, 10.0, start=20)])

    assert state.open is not None
    assert state.open.direction == "below"


@pytest.mark.parametrize("chunk", [1, 3, 7])
def test_continuing_from_saved_state_equals_one_pass(chunk: int) -> None:
    points = [
        *normal(25),
        *series(500.0, start=25),  # ignored outlier
        *normal(10, start=26),
        *series(300.0, 320.0, 100.0, 310.0, start=36),  # an episode with a gap
        *normal(15, start=40),
        *series(20.0, 15.0, 10.0, start=55),  # a second episode, still open at the end
    ]
    one_pass = run_engine(DETECTOR, CONFIG, EngineState(), points)

    closed: list[Episode] = []
    state = EngineState()
    for offset in range(0, len(points), chunk):
        result = run_engine(DETECTOR, CONFIG, state, points[offset : offset + chunk])
        closed.extend(result.closed)
        state = result.state

    assert closed == one_pass.closed
    assert state == one_pass.state
    assert len(closed) == 1
    assert state.open is not None
