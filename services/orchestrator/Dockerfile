FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .

# --prefer-binary: always use pre-built wheels, never compile from source.
# --only-binary=grpcio,grpcio-status,protobuf: hard block on C++ source builds
#   for the three packages that caused 10-min compilation delays.
# --no-deps is NOT used -- transitive deps are already fully pinned in requirements.txt.
RUN pip install --no-cache-dir \
      --prefer-binary \
      --only-binary=grpcio,grpcio-status,protobuf \
      -r requirements.txt

# CACHEBUST: increment to force a clean layer rebuild and guarantee
# the container picks up the latest source tree (no stale main.py).
ARG CACHEBUST=2
COPY . .

# Explicit V23 entrypoint — gunicorn must load main_v23:app, never main:app.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "0", "main_v23:app"]
