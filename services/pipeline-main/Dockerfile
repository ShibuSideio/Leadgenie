FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .

# --prefer-binary: always use pre-built wheels, never compile from source.
# --only-binary=grpcio,grpcio-status,protobuf: hard block on C++ source builds.
RUN pip install --no-cache-dir \
      --prefer-binary \
      --only-binary=grpcio,grpcio-status,protobuf \
      -r requirements.txt

COPY . .

CMD exec gunicorn \
      --config gunicorn.conf.py \
      main_v23:app
