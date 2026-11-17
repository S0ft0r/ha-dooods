ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip \
    && python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir pytapo

ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY service.py /app/service.py

CMD [ "python3", "-u", "/app/service.py" ]
