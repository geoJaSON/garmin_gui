# rasterio/titiler/pingverter ship binary wheels bundling GDAL, so a slim
# Python base is reproducible without a system GDAL. Build tools are present
# only for any source-only deps.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GARMIN_GUI_DATA_DIR=/data

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# constraints.txt pins the exact versions verified end-to-end locally, so the
# image matches the tested environment rather than whatever is latest.
COPY requirements.txt requirements-server.txt constraints.txt ./
RUN pip install -r requirements-server.txt -c constraints.txt

COPY garmin_core/ ./garmin_core/
COPY server/ ./server/
COPY web/ ./web/

VOLUME ["/data"]
EXPOSE 8000

# Single Uvicorn worker: the serial job worker is a background thread inside
# it; multiple web workers would duplicate the queue consumer.
# --proxy-headers + forwarded-allow-ips=*: trust Caddy's X-Forwarded-Proto so
# TiTiler builds https tile URLs (only Caddy can reach :8000 on the internal
# docker network — :8000 is not published).
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
