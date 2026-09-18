FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/research
WORKDIR /srv/research
COPY research-runtime/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY research-runtime/app ./app
RUN useradd --create-home --uid 10002 researcher \
 && mkdir -p /workspace /tmp/research \
 && chown -R researcher:researcher /srv/research /workspace /tmp/research
USER researcher
ENV HOME=/home/researcher TMPDIR=/tmp/research
EXPOSE 8080
CMD ["python", "-m", "app.main"]

