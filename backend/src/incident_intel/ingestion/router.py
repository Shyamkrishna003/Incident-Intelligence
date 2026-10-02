from datetime import timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, Request

from incident_intel.api.deps import get_cache, get_publisher, get_settings_dep, require_scope
from incident_intel.cache.services import CacheServices
from incident_intel.core.config import Settings
from incident_intel.ingestion.normalize import IngestLimits
from incident_intel.ingestion.schemas import IngestAccepted, MetricBatchIn
from incident_intel.ingestion.service import accept_metric_batch
from incident_intel.streaming.producer import MessagePublisher
from incident_intel.tenancy.api_keys import ApiKeyScope
from incident_intel.tenancy.context import TenantContext

router = APIRouter(prefix="/v1/ingest", tags=["ingestion"])

IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,128}$"

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid API key"},
    403: {"description": "API key lacks the ingest:write scope"},
    413: {"description": "Request body too large"},
    409: {"description": "Idempotency-Key already used with different data"},
    422: {"description": "Invalid batch; nothing was accepted"},
    429: {"description": "Rate limit exceeded for this API key; see Retry-After"},
    503: {"description": "Pipeline unavailable; retry with the same Idempotency-Key"},
}


@router.post(
    "/metrics",
    status_code=202,
    response_model=IngestAccepted,
    responses=_ERROR_RESPONSES,
)
async def ingest_metrics(
    request: Request,
    batch: MetricBatchIn,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key",
            pattern=IDEMPOTENCY_KEY_PATTERN,
            description="Unique per batch (e.g. a UUID). Reuse it when retrying the same batch.",
        ),
    ],
    ctx: Annotated[TenantContext, Depends(require_scope(ApiKeyScope.INGEST_WRITE))],
    publisher: Annotated[MessagePublisher, Depends(get_publisher)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    cache: Annotated[CacheServices, Depends(get_cache)],
) -> IngestAccepted:
    """Accept a batch of metric points.

    202 means the batch is durably queued, not yet queryable: it is stored asynchronously,
    usually within a second. The whole batch is accepted or rejected together.
    """
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    accepted = await accept_metric_batch(
        ctx=ctx,
        batch=batch,
        idempotency_key=idempotency_key,
        publisher=publisher,
        idempotency=cache.idempotency,
        topic=request.app.state.metrics_topic,
        limits=IngestLimits(
            max_point_age=timedelta(seconds=settings.ingest_max_point_age_seconds),
            max_future_skew=timedelta(seconds=settings.ingest_max_future_skew_seconds),
        ),
        request_id=request_id if isinstance(request_id, str) else None,
    )
    return IngestAccepted(batch_id=accepted.batch_id, accepted_points=accepted.accepted_points)
