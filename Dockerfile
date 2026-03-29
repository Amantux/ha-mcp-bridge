ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY addon/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY addon/main.py .

# Register with s6-overlay (the HA base image init system, PID 1).
# Never override CMD/ENTRYPOINT — the base image ENTRYPOINT is /init (s6).
# s6 reads services from /etc/services.d/<name>/run and supervises them.
COPY addon/run.sh /etc/services.d/ha-mcp-bridge/run
RUN chmod a+x /etc/services.d/ha-mcp-bridge/run
