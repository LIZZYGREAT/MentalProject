"""Gunicorn defaults for the Docker/server deployment."""

import os


bind = os.getenv("APP_BIND", "0.0.0.0:8000")
workers = int(os.getenv("WEB_CONCURRENCY", "2"))
worker_class = "gthread"
threads = int(os.getenv("WEB_THREADS", "4"))
timeout = int(os.getenv("WEB_TIMEOUT", "180"))
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
capture_output = True
preload_app = False
