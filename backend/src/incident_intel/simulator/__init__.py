"""Scenario simulator: generates clearly labelled synthetic telemetry and sends it through
the real ingestion API, so detection and investigation can be developed and evaluated
without production data. Synthetic data is never presented as real: every metric and log
carries ``source=simulator``, and deployments are marked ``deployed_by=simulator``."""
