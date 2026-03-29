ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Runtime deps:
# - github-cli:  gh auth (device-flow / token storage for sidebar auth flow)
# - nodejs + npm: required to install Copilot CLI via npm (official method)
#   Node.js 22+ is required; use the edge/main repo which tracks latest Node.
# - gcompat: glibc shim (still useful for any native node modules)
RUN apk add --no-cache github-cli git curl bash gcompat && \
    apk add --no-cache nodejs npm --repository=https://dl-cdn.alpinelinux.org/alpine/edge/main/

# Install the official GitHub Copilot CLI via npm.
# This is the recommended install method from the official documentation:
# https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/install-copilot-cli
RUN npm install -g @github/copilot && \
    echo "copilot version: $(copilot --version 2>&1 | head -1)"

COPY addon/run.sh addon/main.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
