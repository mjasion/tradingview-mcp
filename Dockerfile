# ---- Stage 1: Build ----
FROM python:3.14-slim AS builder

# Install system deps and uv
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh

WORKDIR /app

# Copy everything (needed for editable install)
COPY . .

# Install the package and its dependencies into the system Python
RUN uv pip install --system .

# ---- Stage 2: Runtime ----
FROM python:3.14-slim

WORKDIR /app

# Copy installed packages from builder — version-agnostic: copies all of
# /usr/local/lib (contains python3.X/site-packages for whatever X was used).
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/bin/tradingview-mcp /usr/local/bin/tradingview-mcp

# Copy app source (needed for coinlist data files etc.)
COPY --from=builder /app /app

# Create non-root user for security and pre-create the cache directory.
# When docker-compose mounts a named volume here, Docker propagates these
# permissions on first mount — without this mcpuser hits PermissionError.
RUN useradd -m mcpuser \
    && mkdir -p /var/cache/tradingview-mcp \
    && chown -R mcpuser:mcpuser /app /var/cache/tradingview-mcp
USER mcpuser

# Expose the HTTP port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run the MCP server over streamable-http (ideal for Docker/remote deployments)
ENTRYPOINT ["tradingview-mcp"]
CMD ["streamable-http", "--host", "0.0.0.0", "--port", "8000"]
