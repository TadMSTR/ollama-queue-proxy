FROM python:3.12-slim
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
