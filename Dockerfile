# ---- Stage 1: Build ----
FROM python:3.14-slim AS builder

# Install system deps and uv
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh

WORKDIR /app

# Copy everything (needed for editable install)
COPY . .

# Install the exact dependency set from uv.lock, then the package itself.
# `uv pip install --system .` re-resolves from pyproject.toml on every build
# and silently pulls whatever is newest on PyPI — that's how a fresh `mcp`
# major release broke the image while uv.lock still pinned the old one.
# `--frozen` fails the build if uv.lock is out of date instead of re-resolving.
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && uv pip install --system --no-cache --no-deps . \
    && rm /tmp/requirements.txt

# ---- Stage 2: Runtime ----
FROM python:3.14-slim

WORKDIR /app

# Copy installed packages from builder — version-agnostic: copies all of
# /usr/local/lib (contains python3.X/site-packages for whatever X was used).
COPY --from=builder /usr/local/lib /usr/local/lib
COPY --from=builder /usr/local/bin/tradingview-mcp /usr/local/bin/tradingview-mcp

# Copy app source (needed for coinlist data files etc.)
COPY --from=builder /app /app

# Diagnostic tooling for egress/proxy debugging from inside the container.
# The container has its own network namespace, so a proxy reachable from the
# host (curl works there) can still be unreachable here — these let you tell
# DNS failures (dig/nslookup) from routing failures (ip route get / ping) and
# replay the exact proxy probe (curl -x …). See `tradingview-mcp check-proxy`.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl bind9-dnsutils iputils-ping iproute2 netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

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
