FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib \
    APP_ENV=production \
    APP_DATABASE_PATH=/app/data/app.sqlite3

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data /tmp/matplotlib \
    && chown -R appuser:appuser /app /tmp/matplotlib

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:app"]
