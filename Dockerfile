ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Runtime deps:
# - github-cli:  gh auth (device-flow sign-in stored in /data/gh)
# - nodejs + npm: required by the GitHub Copilot CLI (Node.js 22+)
#   Alpine edge/main carries the latest Node LTS.
# - gcompat:     glibc shim (safety net for any native node modules)
RUN apk add --no-cache github-cli git curl gcompat && \
    apk add --no-cache nodejs npm \
        --repository=https://dl-cdn.alpinelinux.org/alpine/edge/main/

# Install the GitHub Copilot CLI using the official npm method.
# Reference: https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli
RUN npm install -g @github/copilot && \
    copilot --version

COPY addon/run.sh addon/main.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
