FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Enterprise-proxy med egen CA: lägg certifikatet i certs/ före bygget
COPY certs/ /usr/local/share/ca-certificates/
RUN update-ca-certificates >/dev/null 2>&1 || true
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

RUN useradd --create-home --uid 1000 app && mkdir -p /data && chown app:app /data
USER app
VOLUME ["/data"]

# Standard: insamlaren. MCP-servern startas med kommandot från compose.
CMD ["forever-ah-collector"]
