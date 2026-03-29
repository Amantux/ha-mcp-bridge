ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY addon/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY addon/ .
RUN chmod +x run.sh

CMD ["/app/run.sh"]
