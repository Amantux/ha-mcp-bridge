ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install gh CLI (Alpine 3.19 community repo has it).
# Also install git (required by gh for some operations).
RUN apk add --no-cache github-cli git

COPY addon/run.sh addon/main.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

# Install the gh-copilot extension into the image so it is available
# without a network call at runtime.  GH_CONFIG_DIR points to a
# throw-away directory during build; the real config (with auth) lives
# at runtime under /data/gh (mapped by Supervisor).
ENV GH_CONFIG_DIR=/tmp/gh-build
RUN gh extension install github/gh-copilot --force || true
# Copy the extension binaries into a fixed location so they survive
# after GH_CONFIG_DIR is changed at runtime.
RUN mkdir -p /app/gh-extensions && \
    cp -r /tmp/gh-build/extensions /app/gh-extensions/ || true

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
