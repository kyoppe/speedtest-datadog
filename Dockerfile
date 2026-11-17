FROM python:3.11-slim

ARG DD_VERSION=dev

# Metadata
LABEL org.opencontainers.image.title="Speedtest to Datadog"
LABEL org.opencontainers.image.description="Periodically runs internet speed tests and sends metrics to Datadog. Supports configurable intervals, server selection with auto-fallback, and observability features."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.version="${DD_VERSION}"

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt requirements-armv7.txt ./

# Use different requirements based on architecture
RUN if [ "$(uname -m)" = "armv7l" ]; then \
        echo "Installing for ARMv7 (without ddtrace)..."; \
        pip install --no-cache-dir -r requirements-armv7.txt; \
    else \
        echo "Installing for $(uname -m)..."; \
        pip install --no-cache-dir -r requirements.txt; \
    fi

# Copy application
COPY speedtest_runner.py .

# Default environment variables
ENV DD_SITE=datadoghq.com \
    METRIC_PREFIX=test.kyouhei \
    SPEEDTEST_FREQUENCY=300 \
    ENABLE_LOGS=false \
    ENABLE_TRACES=false \
    DD_VERSION=${DD_VERSION}

# Health check (check if process is running)
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD pgrep -f speedtest_runner.py || exit 1

# Run the speedtest service
CMD ["python", "-u", "speedtest_runner.py"]

