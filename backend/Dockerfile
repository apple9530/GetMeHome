# Build stage — compiles wheels so the runtime image needs no toolchain.
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libboost-dev libexpat1-dev zlib1g-dev libbz2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libexpat1 zlib1g libbz2-1.0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY src /app/src

WORKDIR /app
ENV PYTHONPATH=/app/src \
    GETMEHOME_DATA_DIR=/data

# The built graph is a volume, not baked into the image: it is regenerated on
# a different cadence than the code and is far larger than the source.
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD python -c "import httpx,sys; sys.exit(0 if httpx.get('http://localhost:8000/health').json()['status']=='ok' else 1)"

CMD ["uvicorn", "getmehome.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
