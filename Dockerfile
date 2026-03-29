ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Only stable Alpine packages — no external downloads at build time.
# github-cli: gh auth device-flow
# nodejs + npm: needed at runtime to install the Copilot CLI
RUN apk add --no-cache github-cli nodejs npm

COPY addon/run.sh addon/main.py addon/requirements.txt addon/copilot-repl.sh ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh ./copilot-repl.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
