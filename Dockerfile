FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=Europe/Moscow

RUN groupadd -r botuser && useradd -r -g botuser botuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data && chown -R botuser:botuser /app/data

USER botuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD kill -0 1 || exit 1

CMD ["python", "bot.py"]
