FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY speedtest_runner.py .

# Default environment variables
ENV DD_SITE=datadoghq.com \
    METRIC_PREFIX=test.kyouhei \
    SPEEDTEST_FREQUENCY=300 \
    ENABLE_LOGS=false \
    ENABLE_TRACES=false

# Health check (check if process is running)
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
    CMD pgrep -f speedtest_runner.py || exit 1

# Run the speedtest service
CMD ["python", "-u", "speedtest_runner.py"]

