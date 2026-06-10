FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential poppler-utils fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY labeler/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY labeler /app/labeler

ENTRYPOINT ["python", "-m", "labeler.cli"]
