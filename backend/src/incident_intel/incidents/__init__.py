"""Incidents: grouping related anomalies so one problem is one incident.

- ``rules``: the pure grouping rules (which incidents an anomaly is related to, and why).
- ``service``: applies the rules against PostgreSQL and records every decision on the
  incident's timeline.
- ``dependencies``: declared "service A depends on service B" relationships.
"""
