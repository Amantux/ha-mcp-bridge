ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Runtime deps:
# - github-cli: gh auth (token storage)
# - curl + bash: copilot install script
# - gcompat + libc6-compat: glibc shim so the Node.js-bundled copilot binary
#   runs on Alpine (which uses musl libc by default)
RUN apk add --no-cache github-cli git curl bash gcompat libc6-compat

# Install the standalone GitHub Copilot CLI.
# The install script downloads copilot-linux-{x64,arm64}.tar.gz from GitHub
# Releases.  armv7 is not published — we allow failure so the build succeeds
# on all arches; main.py will report the binary as missing at runtime.
RUN curl -fsSL https://gh.io/copilot-install | PREFIX=/usr/local bash || \
    echo "copilot install skipped (unsupported arch or network unavailable)"

COPY addon/run.sh addon/main.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
