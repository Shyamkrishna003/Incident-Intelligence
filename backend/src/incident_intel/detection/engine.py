"""Turning point-by-point judgments into anomalies. Pure: no I/O, no clock.

One anomaly ("episode") covers a run of abnormal points on one series, so a ten-minute
problem is one anomaly, not forty alerts.

Rules:

- **Opening.** An episode opens once ``min_consecutive`` points in a row are anomalous. A
  single odd point opens nothing.
- **Extending.** While open, each further anomalous point extends the episode.
- **Closing.** It closes as "recovered" once a normal point arrives at least
  ``close_after`` after the last anomalous one. It closes as "persisted" when it has been
  open for ``max_open``: the level is then treated as the new normal.
- **Baseline.** Values are judged against the last ``window`` *normal* points. Points
  inside an episode (and the anomalous points leading up to one) never enter the baseline,
  so an ongoing problem cannot teach the detector that it is normal. After a "persisted"
  close the baseline starts again from scratch.

``EngineState`` is everything needed to continue later. The database layer rebuilds the
same state from stored rows, so a series evaluated in many small steps gives the same
result as one evaluated in a single pass.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from incident_intel.detection.detectors import Detector, Direction, Verdict

Severity = Literal["low", "medium", "high", "critical"]
ClosedReason = Literal["recovered", "persisted"]


@dataclass(frozen=True)
class Point:
    ts: datetime
    value: float


@dataclass(frozen=True)
class EngineConfig:
    window: int = 120
    min_consecutive: int = 2
    close_after: timedelta = timedelta(minutes=2)
    max_open: timedelta = timedelta(hours=6)


@dataclass(frozen=True)
class Episode:
    # The first anomalous point of the run that opened the episode.
    started_at: datetime
    # When the episode actually opened (the point that completed ``min_consecutive``).
    detected_at: datetime
    last_anomalous_at: datetime
    direction: Direction
    severity: Severity
    peak_score: float
    peak_value: float
    peak_at: datetime
    # What "normal" was when the episode opened.
    baseline_center: float
    baseline_spread: float
    point_count: int
    ended_at: datetime | None = None
    closed_reason: ClosedReason | None = None


@dataclass
class EngineState:
    # The last ``window`` normal points, oldest first.
    baseline: list[Point] = field(default_factory=list)
    # Points whose fate is undecided: the anomalous points of a streak that has not opened
    # an episode yet, or the normal points seen since an open episode's last anomalous one.
    held: list[Point] = field(default_factory=list)
    pending_since: datetime | None = None
    pending_count: int = 0
    open: Episode | None = None


@dataclass
class EngineResult:
    state: EngineState
    # Episodes that closed during this run, oldest first.
    closed: list[Episode] = field(default_factory=list)
    # True when ``state.open`` was opened during this run (rather than carried in).
    opened: bool = False


def severity_for(score: float, threshold: float) -> Severity:
    """A documented rule, not a measured probability: how many times over the detector's
    threshold the worst point was."""
    ratio = abs(score) / threshold if threshold > 0 else 0.0
    if ratio >= 8:
        return "critical"
    if ratio >= 4:
        return "high"
    if ratio >= 2:
        return "medium"
    return "low"


def _extend(episode: Episode, point: Point, verdict: Verdict, threshold: float) -> Episode:
    is_peak = abs(verdict.score) > abs(episode.peak_score)
    return replace(
        episode,
        last_anomalous_at=point.ts,
        point_count=episode.point_count + 1,
        peak_score=verdict.score if is_peak else episode.peak_score,
        peak_value=point.value if is_peak else episode.peak_value,
        peak_at=point.ts if is_peak else episode.peak_at,
        severity=severity_for(max(abs(verdict.score), abs(episode.peak_score)), threshold),
    )


def run_engine(
    detector: Detector,
    config: EngineConfig,
    state: EngineState,
    points: Sequence[Point],
) -> EngineResult:
    """Evaluate ``points`` (oldest first, all newer than anything seen before)."""
    baseline = list(state.baseline)
    held = list(state.held)
    pending_since = state.pending_since
    pending_count = state.pending_count
    current = state.open
    result = EngineResult(state=state)

    def add_normal(items: Sequence[Point]) -> None:
        baseline.extend(items)
        del baseline[: max(0, len(baseline) - config.window)]

    for point in points:
        if current is not None and point.ts - current.started_at >= config.max_open:
            # Open too long: accept the new level as normal and start over.
            result.closed.append(replace(current, ended_at=point.ts, closed_reason="persisted"))
            current = None
            baseline.clear()
            held.clear()
            add_normal([point])
            continue

        verdict = detector.evaluate([item.value for item in baseline], point.value)
        anomalous = verdict is not None and verdict.anomalous

        if anomalous and verdict is not None:
            if current is not None:
                current = _extend(current, point, verdict, detector.threshold)
                held.clear()  # normal points in between are now inside the episode
                continue
            held.append(point)
            pending_count += 1
            pending_since = pending_since or point.ts
            if pending_count >= config.min_consecutive:
                current = Episode(
                    started_at=pending_since,
                    detected_at=point.ts,
                    last_anomalous_at=point.ts,
                    direction=verdict.direction,
                    severity=severity_for(verdict.score, detector.threshold),
                    peak_score=verdict.score,
                    peak_value=point.value,
                    peak_at=point.ts,
                    baseline_center=verdict.center,
                    baseline_spread=verdict.spread,
                    point_count=pending_count,
                )
                result.opened = True
                held.clear()
                pending_since, pending_count = None, 0
            continue

        # A normal point (or not enough history to judge).
        if current is not None:
            if point.ts - current.last_anomalous_at >= config.close_after:
                result.closed.append(replace(current, ended_at=point.ts, closed_reason="recovered"))
                # An episode opened and closed within this run is reported as closed only.
                current = None
                result.opened = False
                add_normal([*held, point])
                held.clear()
            else:
                held.append(point)
            continue
        if pending_count:
            # The streak ended before opening an episode: those points were just noise.
            add_normal(held)
            held.clear()
            pending_since, pending_count = None, 0
        add_normal([point])

    result.state = EngineState(
        baseline=baseline,
        held=held,
        pending_since=pending_since,
        pending_count=pending_count,
        open=current,
    )
    return result
