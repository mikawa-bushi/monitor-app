# Multi-stage build for Monitor App
# Stage 1: Build environment
FROM python:3.11-slim as builder

# Set build arguments
ARG POETRY_VERSION=1.8.3

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies for building
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Poetry
RUN pip install poetry==${POETRY_VERSION}

# Configure Poetry
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VENV_IN_PROJECT=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

# Set work directory
WORKDIR /app

# Copy Poetry files
COPY pyproject.toml poetry.lock ./

# Install dependencies
RUN poetry install --only=main --no-root && rm -rf $POETRY_CACHE_DIR

# Stage 2: Runtime environment
FROM python:3.11-slim as runtime

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Install runtime system dependencies (curl is needed by the healthcheck)
RUN apt-get update && apt-get install -y \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Set work directory
WORKDIR /app

# Copy virtual environment from builder stage
COPY --from=builder /app/.venv /app/.venv

# Copy application code and install the package itself (entry point + package data)
COPY monitor_app/ ./monitor_app/
COPY pyproject.toml poetry.lock README.md LICENSE ./
RUN /app/.venv/bin/pip install --no-deps .

# A monitor-app project (config.py + csv/) is mounted here as a volume.
WORKDIR /project
RUN chown -R appuser:appuser /app /project

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 9990

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:9990/api/health || exit 1

# Default command: serve the mounted project, importing CSV on start.
# Mount your project with: docker run -v $(pwd):/project -p 9990:9990 monitor-app
CMD ["monitor-app", "runserver", "--host", "0.0.0.0", "--port", "9990", "--import-csv"]