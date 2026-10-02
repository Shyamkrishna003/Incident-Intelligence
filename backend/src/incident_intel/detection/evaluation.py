"""Scoring detectors against labelled scenarios.

Every scenario is synthetic and deterministic, with known ground truth: for each series,
the time ranges in which something really was wrong. A detector is run over each series
exactly as in production (same engine), and its anomalies are compared with the truth.

Definitions used in the report:

- A labelled problem is **detected** if at least one anomaly overlaps its time range.
- **Recall** = detected problems / labelled problems.
- An anomaly is a **false positive** if it overlaps no labelled range.
- **Precision** = anomalies that overlap a labelled range / all anomalies.
- **Time to detect** = when the first overlapping anomaly opened, minus the start of the
  labelled range.

These numbers describe behaviour on these synthetic scenarios only. They are not evidence
of performance on real production telemetry.
"""

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from incident_intel.detection.detectors import (
    Detector,
    EwmaDetector,
    RobustZScoreDetector,
    StaticThresholdDetector,
)
from incident_intel.detection.engine import EngineConfig, EngineState, Episode, Point, run_engine
from incident_intel.simulator import scenario as sim

STEP = timedelta(seconds=15)
START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class LabelledSeries:
    name: str
    points: list[Point]
    # Ground truth: when this series really was abnormal. Empty = healthy throughout.
    problems: list[tuple[datetime, datetime]]
    # Its usual level, which the static-threshold rule is set from.
    usual_level: float


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    series: list[LabelledSeries]


DetectorFactory = Callable[[LabelledSeries], Detector]


# --- Scenarios ---------------------------------------------------------------------------


def _simulated(
    name: str, description: str, *, minutes: int, incident_minute: int | None
) -> Scenario:
    incident_at = (
        START + timedelta(minutes=incident_minute) if incident_minute is not None else None
    )
    end = START + timedelta(minutes=minutes)
    series = []
    for spec in sim.METRICS:
        points, at = [], START
        while at < end:
            points.append(Point(at, sim.metric_value(spec, at, incident_at, seed=1)))
            at += STEP
        affected = incident_at is not None and spec.peak != spec.baseline
        problems = []
        if affected and incident_at is not None:
            begins = incident_at + spec.lag
            problems.append((begins, min(end, begins + sim.ROLLBACK_AFTER + sim.RECOVERY)))
        series.append(
            LabelledSeries(f"{spec.service}/{spec.metric}", points, problems, spec.baseline)
        )
    return Scenario(name, description, series)


def _shape(
    name: str,
    description: str,
    level: Callable[[int], float],
    problems: list[tuple[int, int]],
    *,
    minutes: int = 120,
    noise: float = 0.05,
    seed: int = 7,
    outliers: dict[int, float] | None = None,
) -> Scenario:
    """One series whose expected level at each step is ``level(step)`` times 100."""
    points = []
    for step in range(minutes * 4):
        rng = sim._rng(seed, name, step)
        value = 100 * level(step) * (1 + rng.gauss(0, noise))
        if outliers and step in outliers:
            value *= outliers[step]
        points.append(Point(START + step * STEP, max(0.0, value)))
    return Scenario(
        name,
        description,
        [
            LabelledSeries(
                name,
                points,
                [(START + a * STEP, START + b * STEP) for a, b in problems],
                usual_level=100,
            )
        ],
    )


def scenarios() -> list[Scenario]:
    return [
        _simulated(
            "payment_incident",
            "The simulator's incident: 7 series degrade after a deployment, 2 stay healthy.",
            minutes=90,
            incident_minute=45,
        ),
        _simulated(
            "healthy_day",
            "Six hours with no incident. Every anomaly here is a false positive.",
            minutes=360,
            incident_minute=None,
        ),
        _shape(
            "level_shift",
            "The level jumps by 60% and stays there for 30 minutes.",
            lambda step: 1.6 if 240 <= step < 360 else 1.0,
            [(240, 360)],
        ),
        _shape(
            "brief_spike",
            "Triples for one minute (four points).",
            lambda step: 3.0 if 240 <= step < 244 else 1.0,
            [(240, 244)],
        ),
        _shape(
            "gradual_drift",
            "Climbs slowly to double over 40 minutes. Hard: there is no sudden change.",
            lambda step: 1.0 + min(1.0, max(0.0, (step - 200) / 160)),
            [(200, 480)],
        ),
        _shape(
            "drop",
            "Falls to 30% of normal for 15 minutes (for example traffic disappearing).",
            lambda step: 0.3 if 240 <= step < 300 else 1.0,
            [(240, 300)],
        ),
        _shape(
            "single_outliers",
            "Healthy, with five isolated one-point glitches. None is worth an alert.",
            lambda step: 1.0,
            [],
            outliers={90: 4.0, 170: 0.1, 250: 5.0, 330: 3.0, 410: 6.0},
        ),
        _shape(
            "slow_wave",
            "Healthy: load rises and falls by 40% over two hours, like a daily cycle sped up.",
            lambda step: 1.0 + 0.4 * math.sin(2 * math.pi * step / 480),
            [],
            minutes=240,
        ),
        _shape(
            "heavy_tails",
            "Healthy, but one point in twenty is 50% off (bursty, like error counts).",
            lambda step: 1.0,
            [],
            outliers={step: (1.5 if step % 40 else 0.5) for step in range(15, 480, 20)},
        ),
        _shape(
            "noisy_normal",
            "Healthy but four times noisier than usual. Nothing is wrong.",
            lambda step: 1.0,
            [],
            noise=0.2,
        ),
    ]


# --- Scoring -----------------------------------------------------------------------------


def detect_series(
    series: LabelledSeries, detector: Detector, config: EngineConfig
) -> list[Episode]:
    """Every anomaly the detector finds on one series, including one still open at the end."""
    result = run_engine(detector, config, EngineState(), series.points)
    episodes = list(result.closed)
    if result.state.open is not None:
        episodes.append(result.state.open)
    return episodes


def _overlaps(episode: Episode, problem: tuple[datetime, datetime]) -> bool:
    return episode.started_at < problem[1] and episode.last_anomalous_at >= problem[0]


@dataclass
class ScenarioScore:
    scenario: str
    problems: int = 0
    detected: int = 0
    anomalies: int = 0
    false_positives: int = 0
    seconds_to_detect: list[float] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)


@dataclass
class Report:
    detector: str
    scenarios: list[ScenarioScore]

    @property
    def problems(self) -> int:
        return sum(score.problems for score in self.scenarios)

    @property
    def detected(self) -> int:
        return sum(score.detected for score in self.scenarios)

    @property
    def anomalies(self) -> int:
        return sum(score.anomalies for score in self.scenarios)

    @property
    def false_positives(self) -> int:
        return sum(score.false_positives for score in self.scenarios)

    @property
    def recall(self) -> float:
        return self.detected / self.problems if self.problems else math.nan

    @property
    def precision(self) -> float:
        return (
            (self.anomalies - self.false_positives) / self.anomalies if self.anomalies else math.nan
        )

    @property
    def median_seconds_to_detect(self) -> float:
        times = [t for score in self.scenarios for t in score.seconds_to_detect]
        return statistics.median(times) if times else math.nan


def evaluate(
    name: str,
    factory: DetectorFactory,
    config: EngineConfig | None = None,
    cases: list[Scenario] | None = None,
) -> Report:
    config = config or EngineConfig()
    scores = []
    for case in cases if cases is not None else scenarios():
        score = ScenarioScore(case.name)
        for series in case.series:
            episodes = detect_series(series, factory(series), config)
            score.anomalies += len(episodes)
            score.false_positives += sum(
                not any(_overlaps(episode, problem) for problem in series.problems)
                for episode in episodes
            )
            for problem in series.problems:
                score.problems += 1
                hits = [episode for episode in episodes if _overlaps(episode, problem)]
                if hits:
                    score.detected += 1
                    first = min(hits, key=lambda episode: episode.detected_at)
                    score.seconds_to_detect.append(
                        max(0.0, (first.detected_at - problem[0]).total_seconds())
                    )
                else:
                    score.missed.append(series.name)
        scores.append(score)
    return Report(name, scores)


def candidates(threshold: float, min_history: int) -> dict[str, DetectorFactory]:
    """The detectors compared in the report."""
    return {
        # The simple rule: alert when a metric is more than double its usual level.
        "static_threshold (2x usual)": lambda series: StaticThresholdDetector(
            upper=2 * series.usual_level
        ),
        "ewma": lambda _series: EwmaDetector(threshold=threshold, min_history=min_history),
        "robust_zscore": lambda _series: RobustZScoreDetector(
            threshold=threshold, min_history=min_history
        ),
    }


def format_report(reports: list[Report]) -> str:
    def number(value: float, pattern: str) -> str:
        return "n/a" if math.isnan(value) else pattern.format(value)

    header = ("detector", "recall", "precision", "false +", "median time to detect")
    lines = [f"{header[0]:<28} {header[1]:>8} {header[2]:>10} {header[3]:>8} {header[4]:>22}"]
    for report in reports:
        lines.append(
            f"{report.detector:<28} "
            f"{f'{report.detected}/{report.problems}':>8} "
            f"{number(report.precision, '{:.0%}'):>10} "
            f"{report.false_positives:>8} "
            f"{number(report.median_seconds_to_detect, '{:.0f} s'):>22}"
        )
    for report in reports:
        lines.append(f"\n{report.detector}, by scenario:")
        for score in report.scenarios:
            missed = f"  missed: {', '.join(score.missed)}" if score.missed else ""
            lines.append(
                f"  {score.scenario:<18} detected {score.detected}/{score.problems}, "
                f"{score.false_positives} false positive(s){missed}"
            )
    return "\n".join(lines)
