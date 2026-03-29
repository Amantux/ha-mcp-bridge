ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Copy add-on source files
COPY addon/run.sh addon/main.py addon/auth.py addon/copilot.py addon/requirements.txt ./
COPY addon/static/ ./static/

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
