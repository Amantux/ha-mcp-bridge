ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Runtime deps:
# - github-cli:         gh auth (device-flow / token storage)
# - curl + bash:        copilot download + scripts
# - gcompat:            glibc ABI compatibility shim (needed by Node.js-bundled copilot)
# - libc6-compat:       /lib/libc.musl-* → libgcc_s.so.1 symlinks
RUN apk add --no-cache github-cli git curl bash gcompat libc6-compat

# Install the standalone GitHub Copilot CLI.
# Downloads copilot-linux-{x64,arm64}.tar.gz directly from GitHub Releases
# (no install script — avoids SHA256 checksum step that exits 1 on flaky nets).
# The tarball contains a single top-level file named `copilot`.
# armv7 has no release — we gracefully skip; main.py retries at runtime.
RUN set -e; \
    ARCH=$(uname -m); \
    case "$ARCH" in \
      x86_64|amd64)    DL_ARCH="x64"   ;; \
      aarch64|arm64)   DL_ARCH="arm64" ;; \
      *)               echo "Arch $ARCH: no copilot release, skipping"; exit 0 ;; \
    esac; \
    URL="https://github.com/github/copilot-cli/releases/latest/download/copilot-linux-${DL_ARCH}.tar.gz"; \
    echo "==> Downloading $URL"; \
    curl -fsSL --retry 3 --retry-delay 2 "$URL" -o /tmp/copilot.tar.gz; \
    echo "==> Downloaded $(wc -c < /tmp/copilot.tar.gz) bytes"; \
    echo "==> Tarball contents: $(tar -tzf /tmp/copilot.tar.gz)"; \
    tar -xzf /tmp/copilot.tar.gz -C /usr/local/bin/; \
    chmod +x /usr/local/bin/copilot; \
    rm -f /tmp/copilot.tar.gz; \
    echo "==> copilot version: $(copilot --version 2>&1 | head -1 || echo 'version check failed (may need runtime glibc)')"

COPY addon/run.sh addon/main.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
