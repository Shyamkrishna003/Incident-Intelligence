"""Anomaly detection: deciding which metric points are abnormal, and grouping them into
anomalies.

- ``detectors``: pure functions that judge one value against a baseline window.
- ``engine``: pure logic that turns a stream of judgments into anomalies that open, extend,
  and close.
- ``service``: loads state from PostgreSQL, runs the engine, and stores the result.
- ``consumer``: runs the service whenever new metric points have been stored.
- ``evaluation``: scores detectors against labelled scenarios.
"""
