FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install functions-framework

COPY . .

CMD exec functions-framework --target=trigger_daily_sweep --port=$PORT
