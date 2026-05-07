# Multi-stage build for drop-pensa

FROM python:3.12-slim AS builder

WORKDIR /build

# Install uv
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Copy project files
COPY pyproject.toml .

# Install dependencies with uv
ENV PATH="/root/.local/bin:$PATH"
RUN uv venv && uv pip install --quiet .

# Runtime stage
FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies (libmagic for python-magic)
RUN apt-get update && apt-get install -y --no-install-recommends libmagic1 && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /build/.venv /app/.venv

# Copy application code
COPY src/drop_pensa /app/drop_pensa

# Create storage and db directories
RUN mkdir -p /var/lib/drop-pensa/files && chmod 755 /var/lib/drop-pensa

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

CMD ["python", "-m", "drop_pensa"]
