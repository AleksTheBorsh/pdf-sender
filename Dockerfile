FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY templates ./templates

EXPOSE 5001

CMD ["gunicorn", "-b", "0.0.0.0:5001", "-w", "2", "--timeout", "120", "app:app"]
