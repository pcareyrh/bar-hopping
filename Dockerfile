FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && rm -rf /root/.cache/pip /tmp/* /var/tmp/*

COPY app/ app/

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

CMD ["rq", "worker", "default"]
