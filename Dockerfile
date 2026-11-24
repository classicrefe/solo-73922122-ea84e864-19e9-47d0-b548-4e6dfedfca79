FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/survey.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# Two worker processes so a long incremental upload (CPU-bound weight
# generation) cannot block queries/corrections in the other worker.  SQLite
# consistency is kept by WAL + BEGIN IMMEDIATE transactions and the
# generation/fingerprint baseline check, which also covers cross-worker
# upload races.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "600", "wsgi:app"]
