FROM mcr.microsoft.com/playwright/python:v1.65.1-jammy
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 4 --timeout 600
