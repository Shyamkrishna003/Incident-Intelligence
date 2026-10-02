#!/usr/bin/env bash
# Send a small, synthetic metric batch to a local Incident Intelligence API and read it back.
#
#   KEY=$(make -s bootstrap ORG=acme PROJECT=payments) ./examples/send-metrics.sh
#
# Timestamps are generated relative to now, because the API only accepts points from the
# last 7 days (and at most 5 minutes in the future). The values are illustrative only.
set -euo pipefail

: "${KEY:?set KEY to a project API key (ii_...)}"
API="${API:-http://localhost:8000}"
SERVICE="${SERVICE:-payment-api}"
METRIC="${METRIC:-http.server.duration.p95}"

ts() { date -u -d "$1" +%Y-%m-%dT%H:%M:%SZ; }

body=$(cat <<JSON
{
  "points": [
    {"service": "$SERVICE", "metric": "$METRIC", "unit": "ms", "timestamp": "$(ts '-3 min')", "value": 120, "attributes": {"region": "eu-west-1"}},
    {"service": "$SERVICE", "metric": "$METRIC", "unit": "ms", "timestamp": "$(ts '-2 min')", "value": 150, "attributes": {"region": "eu-west-1"}},
    {"service": "$SERVICE", "metric": "$METRIC", "unit": "ms", "timestamp": "$(ts '-1 min')", "value": 800, "attributes": {"region": "eu-west-1"}},
    {"service": "$SERVICE", "metric": "$METRIC", "unit": "ms", "timestamp": "$(ts 'now')", "value": 1700, "attributes": {"region": "eu-west-1"}}
  ]
}
JSON
)

# One key per logical batch. Reuse the same key if you retry this exact batch.
idempotency_key="example-$(date +%s)-$RANDOM"

echo "POST $API/v1/ingest/metrics (Idempotency-Key: $idempotency_key)"
curl -sS -X POST "$API/v1/ingest/metrics" \
  -H "Authorization: Bearer $KEY" \
  -H "Idempotency-Key: $idempotency_key" \
  -H "Content-Type: application/json" \
  -d "$body"
echo

# Storage is asynchronous (Kafka -> storage consumer -> PostgreSQL); give it a moment.
sleep 2

echo "GET $API/v1/services/$SERVICE/metrics/$METRIC"
curl -sS -H "Authorization: Bearer $KEY" "$API/v1/services/$SERVICE/metrics/$METRIC"
echo
