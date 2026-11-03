# Build from the repository root:  docker compose build
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Python packages feed plugins need (CHART_EXTRA_PIP in .env, e.g. "databento"; docs/FEEDS.md, Packages)
ARG CHART_EXTRA_PIP=""
RUN if [ -n "$CHART_EXTRA_PIP" ]; then pip install --no-cache-dir $CHART_EXTRA_PIP; fi
COPY server/chartsrv ./chartsrv
COPY web ./web
COPY config ./config
COPY ninjatrader/TatankaChartFeed.cs ./ninjatrader/
ENV CHART_WEB_DIR=/app/web CHART_CONFIG_DIR=/app/config CHART_DB=/data/chart.db CHART_FEEDS_DIR=/feeds
# packages installed from Connections (a plugin's Install button): in the data volume, so they stay through rebuilds;
# put on the import path after the image's own (packages.use_user_site), never in front of them
ENV PYTHONUSERBASE=/data/pylib PYTHONNOUSERSITE=1
EXPOSE 8800
# the commit this was built from (update.cmd passes it): Connections > Updates compares it with GitHub's. Last, so a new
# commit rebuilds only this layer.
ARG CHART_COMMIT=""
ENV CHART_COMMIT=$CHART_COMMIT
CMD ["uvicorn", "chartsrv.app:app", "--host", "0.0.0.0", "--port", "8800", "--loop", "asyncio", "--proxy-headers"]
