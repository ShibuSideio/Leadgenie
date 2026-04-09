FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 1 worker, 4 threads — DT calls are I/O-bound (Serper + Vertex).
# timeout=0: Cloud Run handles request-level timeouts externally.
CMD exec gunicorn --bind :$PORT --workers 1 --threads 4 --timeout 0 main:app
