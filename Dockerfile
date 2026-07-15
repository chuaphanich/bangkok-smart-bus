FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

COPY requirements_bangkok_bus.txt .
RUN pip install --no-cache-dir -r requirements_bangkok_bus.txt

COPY . .

RUN python scripts/ensure_models.py

EXPOSE 10000

CMD gunicorn -b 0.0.0.0:${PORT} -w 1 --timeout 180 --access-logfile - app:app
