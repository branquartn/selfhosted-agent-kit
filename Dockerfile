# SelfHosted Agent Kit — minimal, non-root image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    AGENT_ENV_FILE=/app/.env \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8800 \
    AGENT_DATA_DIR=/data \
    AGENT_WORKSPACE=/workspace \
    AGENT_WEB_DIR=/app/web

# git and ripgrep are handy for the agent's tools; drop them if you don't need them.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ripgrep \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY src/ /app/src/
COPY web/ /app/web/
COPY pyproject.toml README.md LICENSE /app/

RUN useradd --system --create-home --uid 10001 agent \
 && mkdir -p /data /workspace \
 && chown -R agent:agent /data /workspace /app

USER agent
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/healthz', timeout=2).status==200 else 1)"

CMD ["python3", "-m", "agent.server"]
