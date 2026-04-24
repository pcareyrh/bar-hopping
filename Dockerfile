FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /root/.cache/pip /tmp/* /var/tmp/*

COPY app/ app/

CMD ["rq", "worker", "default"]
