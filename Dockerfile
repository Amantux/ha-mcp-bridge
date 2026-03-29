ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Runtime deps:
# - github-cli:   gh auth (token storage / device-flow)
# - curl + bash:  copilot download
# - gcompat + libc6-compat: glibc shim for Node.js-bundled copilot binary on Alpine (musl)
RUN apk add --no-cache github-cli git curl bash gcompat libc6-compat

# Install the standalone GitHub Copilot CLI directly from GitHub Releases.
# We bypass the install script (which runs SHA256 validation that can fail on
# flaky networks) and download + extract the tarball ourselves.
# armv7 has no published release — we allow failure so the build succeeds;
# main.py will retry at runtime for amd64 / aarch64.
RUN set -e; \
    ARCH=$(uname -m); \
    case "$ARCH" in \
      x86_64|amd64)    DL_ARCH="x64"   ;; \
      aarch64|arm64)   DL_ARCH="arm64" ;; \
      *) echo "Arch $ARCH has no copilot release — skipping"; exit 0 ;; \
    esac; \
    URL="https://github.com/github/copilot-cli/releases/latest/download/copilot-linux-${DL_ARCH}.tar.gz"; \
    echo "Downloading $URL"; \
    curl -fsSL "$URL" -o /tmp/copilot.tar.gz; \
    tar -xzf /tmp/copilot.tar.gz -C /tmp; \
    BINARY=$(find /tmp -maxdepth 3 -name "copilot" -type f | head -1); \
    if [ -n "$BINARY" ]; then \
      mv "$BINARY" /usr/local/bin/copilot; \
      chmod +x /usr/local/bin/copilot; \
      echo "copilot installed: $(copilot --version 2>&1 | head -1)"; \
    else \
      echo "ERROR: copilot binary not found in tarball"; exit 1; \
    fi; \
    rm -f /tmp/copilot.tar.gz

COPY addon/run.sh addon/main.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
