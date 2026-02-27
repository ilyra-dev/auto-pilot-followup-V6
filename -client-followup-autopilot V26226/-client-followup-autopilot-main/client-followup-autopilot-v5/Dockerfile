FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY tools/ tools/
COPY workflows/ workflows/
COPY CLAUDE.md .

# Create required directories
RUN mkdir -p .tmp/style_data tokens

# Set working directory to tools (scripts use relative imports)
WORKDIR /app/tools

EXPOSE 3000

# Health check
HEALTHCHECK --interval=120s --timeout=10s --start-period=30s --retries=5 \
    CMD python health_check.py --json || exit 1

# Run daemon
# CMD ["python", "daemon_main.py"]
CMD sh -c 'echo "$FILE_CREDENTIALS" > /app/service_account.json && python daemon_main.py'
