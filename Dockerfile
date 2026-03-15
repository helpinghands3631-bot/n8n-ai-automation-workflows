# Dockerfile - KeyForAgents.com & Helping Hands Backend
# Multi-stage build for minimal production image

# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Production stage ----
FROM python:3.11-slim AS production

LABEL maintainer="dean@helpinghands.com.au"
LABEL org.opencontainers.image.source="https://github.com/helpinghands3631-bot/n8n-ai-automation-workflows"
LABEL org.opencontainers.image.description="KeyForAgents.com AI automation backend"

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy backend source
COPY backend/ ./backend/

# Non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Environment defaults (override via docker run -e or .env)
ENV FLASK_ENV=production \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000

EXPOSE 5000 5001 5002

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:$PORT/health', timeout=5)" || exit 1

# Default: run webhook receiver (override with CMD at runtime)
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5002", "backend.webhook_receiver:app", "--access-logfile", "-"]
