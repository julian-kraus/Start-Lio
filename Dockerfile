FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACKER_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tracker ./tracker
COPY kb ./kb
COPY mail ./mail
COPY structured ./structured
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh \
    && useradd --uid 10001 --create-home tracker \
    && mkdir -p /data && chown tracker:tracker /data

# Starts as root only to read the data directory's owner, then drops to that user (see the entrypoint).
ENTRYPOINT ["docker-entrypoint.sh"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "-m", "tracker", "healthcheck"]

CMD ["python", "-m", "tracker", "run"]
