FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# apt-get update must run in the same layer as playwright --with-deps
RUN apt-get update && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/*

COPY app/ app/

RUN mkdir -p data

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
