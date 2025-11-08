# Multi-stage build for smaller final image
FROM python:3.12-slim as builder

WORKDIR /app

# Install system dependencies needed for Pillow and other libraries
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    libfreetype6-dev \
    liblcms2-dev \
    libopenjp2-7-dev \
    libtiff-dev \
    libwebp-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Final stage
FROM python:3.12-slim

WORKDIR /app

# Install runtime dependencies for Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libjpeg62-turbo \
    zlib1g \
    libpng16-16 \
    libfreetype6 \
    liblcms2-2 \
    libopenjp2-7 \
    libtiff6 \
    libwebp7 \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from builder
COPY --from=builder /root/.local /root/.local

# Copy the MCP server source
COPY src/ ./src/

# Create non-root user for security
RUN useradd -m -u 1000 odoo && chown -R odoo:odoo /app
USER odoo

# Set environment variables (can be overridden at runtime)
ENV PATH=/root/.local/bin:$PATH
ENV ODOO_URL=http://localhost:8069
ENV ODOO_DATABASE=""
ENV ODOO_API_KEY=""
ENV ODOO_REQUEST_TIMEOUT=30
ENV ODOO_MAX_RETRIES=3

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# Run the MCP server
CMD ["python", "src/odoo_mcp_server.py"]
