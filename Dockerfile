# Digest-pinned, not tag-only: a tag floats, so two builds of the same commit can
# produce different images and the provenance attestation in release.yml then
# attests something not reproducible. Dependabot's `docker` ecosystem bumps this
# digest weekly, which is what keeps the pin from going stale (DC-01).
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# Apply available OS security updates before anything else.
#
# The base image lags Debian's security archive, and its CVEs become this image's
# CVEs the moment it is published — there is nobody downstream to inherit the
# problem. Measured on 2026-09-11, python:3.12-slim carried 4 fixable HIGH CVEs
# (openssl CVE-2026-14456; util-linux CVE-2026-53612/53613/53614) across 30
# package instances, every one of them with a fix already in the archive.
#
# This is what makes the Trivy gate in release.yml a gate rather than a
# permanent red light. Without it the scan fails on the first tag, and a check
# that has never once passed gets bypassed rather than fixed.
RUN apt-get update && \
    apt-get upgrade -y --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir . && \
    adduser --disabled-password --gecos "" --uid 1000 appuser
USER appuser
EXPOSE 11435
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:11435/health')" || exit 1
CMD ["ollama-queue-proxy"]
