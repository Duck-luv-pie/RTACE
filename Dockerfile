# One image for every RTACE Python service; the compose file picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

RUN useradd --system --uid 10001 --no-create-home rtace && chown -R rtace:rtace /app
USER rtace

CMD ["python", "-m", "detection_engine.consumer"]
