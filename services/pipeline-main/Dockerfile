FROM python:3.11-slim

WORKDIR /app
COPY services/pipeline-main/requirements.txt .

# --prefer-binary: always use pre-built wheels, never compile from source.
# --only-binary=grpcio,grpcio-status,protobuf: hard block on C++ source builds.
RUN pip install --no-cache-dir \
      --prefer-binary \
      --only-binary=grpcio,grpcio-status,protobuf \
      -r requirements.txt

COPY services/pipeline-main .
COPY services/shared ./shared
# V27 IntentDomainOrchestrator — single brain package
COPY services/intelligence ./intelligence

CMD exec gunicorn \
      --config gunicorn.conf.py \
      main_v23:app
