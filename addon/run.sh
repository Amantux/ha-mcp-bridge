#!/usr/bin/with-contenv bashio
# with-contenv exports s6-overlay container environment variables
# (including SUPERVISOR_TOKEN) before exec-ing the service.
# Without it, SUPERVISOR_TOKEN is not visible to Python.
set -e
exec python3 /app/main.py
