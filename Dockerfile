FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY auth.py bot.py cache.py check_setup.py config.py graph_client.py healthcheck.py matching.py notifier.py \
     planner_service.py state.py ./

# Run as an unprivileged user. /data holds the SQLite state and must be a volume.
RUN groupadd --system --gid 10001 bot \
    && useradd --system --uid 10001 --gid bot --no-create-home bot \
    && mkdir /data \
    && chown bot:bot /data
USER bot
VOLUME /data

HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 CMD ["python", "healthcheck.py"]

CMD ["python", "bot.py"]
