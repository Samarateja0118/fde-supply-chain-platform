# syntax=docker/dockerfile:1
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app
COPY pyproject.toml README.md ./
COPY src ./src
# Optional secret for builds behind a TLS-intercepting proxy: --secret id=ca_bundle,src=ca.crt
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi; \
    pip install --no-cache-dir . && mkdir -p /work && chown app /work
USER app
WORKDIR /work
ENTRYPOINT ["fde"]
