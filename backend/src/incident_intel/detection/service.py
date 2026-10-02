"""Running detection for a series against PostgreSQL.

For one series this loads where detection left off, evaluates the points stored since
then with the pure engine, and writes the resulting anomalies and the new position. The
caller owns the transaction, so all of it commits together or not at all.

The engine state is rebuilt from stored rows each time (see ``_load_state``); it matches
the state the engine would hold in memory, so evaluating a series in many small steps
gives the same anomalies as evaluating it in one pass.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import structlog
from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from incident_intel.core.config import RuntimeSettings
from incident_intel.detection.detectors import Detector, build_detector
from incident_intel.detection.engine import (
    EngineConfig,
    EngineState,
    Episode,
    Point,
    run_engine,
)
from incident_intel.detection.models import Anomaly, DetectionState
from incident_intel.telemetry.models import MetricPoint, MetricSeries

logger = structlog.get_logger(__name__)

# Points evaluated per pass. A series with more new points than this is evaluated in
# several passes within the same call.
_CHUNK = 5000


@dataclass(frozen=True)
class DetectionSettings:
    detector: Detector
    engine: EngineConfig

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> "DetectionSettings":
        return cls(
            detector=build_detector(
                settings.detection_detector,
                threshold=settings.detection_threshold,
                min_history=settings.detection_min_history,
            ),
            engine=EngineConfig(
                window=settings.detection_window,
                min_consecutive=settings.detection_min_consecutive,
                close_after=timedelta(seconds=settings.detection_close_after_seconds),
                max_open=timedelta(seconds=settings.detection_max_open_seconds),
            ),
        )


@dataclass
class SeriesOutcome:
    evaluated_points: int = 0
    opened: int = 0
    closed: int = 0


def _episode(row: Anomaly) -> Episode:
    return Episode(
        started_at=row.started_at,
        detected_at=row.detected_at,
        last_anomalous_at=row.last_anomalous_at,
        direction=row.direction,  # type: ignore[arg-type]  # constrained by the database
        severity=row.severity,  # type: ignore[arg-type]
        peak_score=row.peak_score,
        peak_value=row.peak_value,
        peak_at=row.peak_at,
        baseline_center=row.baseline_center,
        baseline_spread=row.baseline_spread,
        point_count=row.point_count,
    )


def _apply(row: Anomaly, episode: Episode) -> None:
    row.status = "open" if episode.ended_at is None else "closed"
    row.direction = episode.direction
    row.severity = episode.severity
    row.started_at = episode.started_at
    row.detected_at = episode.detected_at
    row.last_anomalous_at = episode.last_anomalous_at
    row.ended_at = episode.ended_at
    row.closed_reason = episode.closed_reason
    row.peak_score = episode.peak_score
    row.peak_value = episode.peak_value
    row.peak_at = episode.peak_at
    row.baseline_center = episode.baseline_center
    row.baseline_spread = episode.baseline_spread
    row.point_count = episode.point_count


async def _points(
    session: AsyncSession,
    series: MetricSeries,
    *conditions: object,
    newest_first: bool = False,
    limit: int,
) -> list[Point]:
    order = MetricPoint.ts.desc() if newest_first else MetricPoint.ts
    rows = await session.execute(
        select(MetricPoint.ts, MetricPoint.value)
        .where(
            MetricPoint.project_id == series.project_id,
            MetricPoint.series_id == series.id,
            *conditions,  # type: ignore[arg-type]
        )
        .order_by(order)
        .limit(limit)
    )
    points = [Point(ts, value) for ts, value in rows]
    return points[::-1] if newest_first else points


async def _load_state(
    session: AsyncSession,
    series: MetricSeries,
    detector: Detector,
    config: EngineConfig,
    state: DetectionState | None,
    open_row: Anomaly | None,
) -> EngineState:
    if state is None:
        return EngineState()
    watermark = state.evaluated_through
    pending_since = state.pending_since if state.pending_count else None
    # The baseline is the last `window` normal points: evaluated, before any undecided
    # streak or open anomaly, after the latest "persisted" close, and outside every
    # recovered anomaly.
    before = open_row.started_at if open_row is not None else pending_since
    restart = await session.scalar(
        select(func.max(Anomaly.ended_at)).where(
            Anomaly.series_id == series.id,
            Anomaly.detector == detector.name,
            Anomaly.closed_reason == "persisted",
        )
    )
    inside_recovered = exists().where(
        Anomaly.series_id == MetricPoint.series_id,
        Anomaly.detector == detector.name,
        Anomaly.closed_reason == "recovered",
        MetricPoint.ts >= Anomaly.started_at,
        MetricPoint.ts <= Anomaly.last_anomalous_at,
    )
    baseline = await _points(
        session,
        series,
        MetricPoint.ts <= watermark,
        *([MetricPoint.ts < before] if before is not None else []),
        *([MetricPoint.ts >= restart] if restart is not None else []),
        ~inside_recovered,
        newest_first=True,
        limit=config.window,
    )
    # Undecided points: normal points since an open anomaly's last anomalous one, or the
    # anomalous points of a streak that has not opened an anomaly yet.
    if open_row is not None:
        held_from = MetricPoint.ts > open_row.last_anomalous_at
    elif pending_since is not None:
        held_from = MetricPoint.ts >= pending_since
    else:
        held_from = None
    held = (
        await _points(session, series, held_from, MetricPoint.ts <= watermark, limit=_CHUNK)
        if held_from is not None
        else []
    )
    return EngineState(
        baseline=baseline,
        held=held,
        pending_since=pending_since,
        pending_count=state.pending_count if pending_since is not None else 0,
        open=_episode(open_row) if open_row is not None else None,
    )


async def _detect_once(
    session: AsyncSession, series: MetricSeries, settings: DetectionSettings, outcome: SeriesOutcome
) -> bool:
    """Evaluate the next chunk of new points. Returns True if there may be more."""
    detector = settings.detector
    state = await session.get(DetectionState, (series.id, detector.name))
    after = state.evaluated_through if state is not None else None
    new_points = await _points(
        session,
        series,
        *([MetricPoint.ts > after] if after is not None else []),
        limit=_CHUNK,
    )
    if not new_points:
        return False

    open_row = await session.scalar(
        select(Anomaly).where(
            Anomaly.project_id == series.project_id,
            Anomaly.series_id == series.id,
            Anomaly.detector == detector.name,
            Anomaly.status == "open",
        )
    )
    engine_state = await _load_state(session, series, detector, settings.engine, state, open_row)
    result = run_engine(detector, settings.engine, engine_state, new_points)

    # The carried-in open anomaly is updated in place; everything else is a new row.
    carried_start = open_row.started_at if open_row is not None else None
    for episode in [*result.closed, *([result.state.open] if result.state.open else [])]:
        if open_row is not None and episode.started_at == carried_start:
            _apply(open_row, episode)
        else:
            row = Anomaly(
                organization_id=series.organization_id,
                project_id=series.project_id,
                series_id=series.id,
                detector=detector.name,
                detector_version=detector.version,
            )
            _apply(row, episode)
            session.add(row)
            outcome.opened += 1
        if episode.ended_at is not None:
            outcome.closed += 1
        # Flush in order: a closed row must be written before a new open one for the same
        # series, or the "one open anomaly per series" index would reject it.
        await session.flush()

    await session.execute(
        pg_insert(DetectionState)
        .values(
            series_id=series.id,
            detector=detector.name,
            project_id=series.project_id,
            evaluated_through=new_points[-1].ts,
            pending_since=result.state.pending_since,
            pending_count=result.state.pending_count,
        )
        .on_conflict_do_update(
            index_elements=[DetectionState.series_id, DetectionState.detector],
            set_={
                "evaluated_through": new_points[-1].ts,
                "pending_since": result.state.pending_since,
                "pending_count": result.state.pending_count,
            },
        )
    )
    # The state row may be loaded in this session; make the next read see the new values.
    if state is not None:
        await session.refresh(state)
    outcome.evaluated_points += len(new_points)
    return len(new_points) == _CHUNK


async def detect_series(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    series_id: uuid.UUID,
    settings: DetectionSettings,
) -> SeriesOutcome:
    """Evaluate every point of the series stored since it was last evaluated.

    Does not commit. A series that does not exist in the project is ignored (the event
    that named it is then simply stale or wrong).
    """
    outcome = SeriesOutcome()
    series = await session.scalar(
        select(MetricSeries).where(
            MetricSeries.project_id == project_id, MetricSeries.id == series_id
        )
    )
    if series is None:
        logger.warning("detection_series_not_found", series_id=str(series_id))
        return outcome
    while await _detect_once(session, series, settings, outcome):
        pass
    return outcome
