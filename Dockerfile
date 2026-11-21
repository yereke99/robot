FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    JAVA_BIN=/usr/lib/jvm/java-21-openjdk-amd64/bin/java

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        openjdk-21-jre-headless \
        ca-certificates \
        tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Explicit copy of the goszakup package + the Kalkan jars (some users
# strip jars/ via .gitignore, but they MUST live in the image — the Java
# helper can't sign without them).
COPY goszakup/ ./goszakup/
COPY goszakup/jars/ ./goszakup/jars/
COPY submit.py ./

RUN mkdir -p /data /secrets \
 && useradd -u 1000 -m bot \
 && chown -R bot:bot /app /data

USER bot
ENV GOSZAKUP_STATE_DIR=/data/state \
    GOSZAKUP_LOG_DIR=/data/logs \
    GOSZAKUP_HISTORY_DB=/data/bot_history.db \
    GOSZAKUP_SECRETS_FILE=/data/secrets.json

# tini handles SIGTERM cleanly so KalkanSigner can flush + exit
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "goszakup.bot.main"]
