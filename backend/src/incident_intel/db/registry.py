"""Import every model module so ``Base.metadata`` is complete (used by Alembic and tests)."""

from incident_intel.audit import models as audit_models
from incident_intel.db.base import Base
from incident_intel.detection import models as detection_models
from incident_intel.incidents import models as incident_models
from incident_intel.investigation import models as investigation_models
from incident_intel.learning import models as learning_models
from incident_intel.telemetry import models as telemetry_models
from incident_intel.tenancy import models as tenancy_models

__all__ = [
    "Base",
    "audit_models",
    "detection_models",
    "incident_models",
    "investigation_models",
    "learning_models",
    "telemetry_models",
    "tenancy_models",
]
