ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

COPY addon/run.sh addon/main.py addon/requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt && \
    chmod +x ./run.sh

EXPOSE 8099

ENTRYPOINT ["./run.sh"]
