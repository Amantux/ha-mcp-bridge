ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install gh CLI (for auth), curl + bash (for copilot install), git.
RUN apk add --no-cache github-cli git curl bash

# Install the new standalone GitHub Copilot CLI.
# This replaces the deprecated gh-copilot extension.
# https://github.com/github/copilot-cli
# The install script detects linux/amd64|arm64|armv7 and places
# the `copilot` binary in /usr/local/bin/copilot.
RUN curl -fsSL https://gh.io/copilot-install | PREFIX=/usr/local bash

COPY addon/run.sh addon/main.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
