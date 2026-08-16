FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    WATCH_HOST=0.0.0.0 \
    WATCH_PORT=8788 \
    WATCHLIST_FILE=/tmp/watchtower/watchlist.json \
    WATCH_POSITION_FILE=/tmp/watchtower/positions.json \
    WATCH_INTRADAY_DB_FILE=/tmp/watchtower/runtime/intraday_watchtower.sqlite \
    WATCH_AUCTION_HISTORY_FILE=/tmp/watchtower/runtime/auction_snapshots.jsonl \
    WATCH_OPENING_DECISION_FILE=/tmp/watchtower/runtime/opening_decisions.jsonl \
    WATCH_BACKGROUND_COLLECTOR=1 \
    WATCH_OPENING_WINDOW_ENGINE=1 \
    WATCH_PERSISTENCE_BACKEND=cloudbase_nosql \
    WATCH_CLOUDBASE_ENV_ID=server-d2g7x597t019f5cb0 \
    WATCH_CLOUDBASE_STATE_COLLECTION=watchtower_state \
    WATCH_MESSAGE_STORE_BACKEND=cloudbase_mysql \
    WATCH_CLOUDBASE_MYSQL_INSTANCE=default \
    WATCH_CLOUDBASE_MYSQL_SCHEMA=server-d2g7x597t019f5cb0 \
    WATCH_DARK_POOL=1

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY web/dist ./web/dist
COPY data/themes.yaml ./data/themes.yaml
COPY data/trading_rules.yaml ./data/trading_rules.yaml
RUN mkdir -p /tmp/watchtower/runtime \
    && printf '[]\n' > /tmp/watchtower/watchlist.json \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir .
EXPOSE 8788
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8788"]
