"""Redis-backed fast state: rate limits, idempotency claims, and the API-key cache.

Redis is never the source of truth. Every component here degrades to a safe fallback when
Redis is unreachable (see each module), so a Redis outage slows nothing down for long and
loses no data.
"""
