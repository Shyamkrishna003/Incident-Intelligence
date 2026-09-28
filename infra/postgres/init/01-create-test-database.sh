#!/bin/sh
# Runs once, when the compose volume is first initialized.
# Creates a separate database for integration tests (name must end in _test).
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -c "CREATE DATABASE \"${POSTGRES_DB}_test\""
