FROM python:3.12-slim

WORKDIR /app

# Abhaengigkeiten zuerst (besseres Layer-Caching)
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Anwendung + Generator-HTML
COPY app/ /app/
COPY site/ /app/site/

ENV CONFIG_DIR=/config \
    SITE_DIR=/app/site \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# 2 Worker; Config wird pro Request aus dem Volume gelesen -> konsistent ueber Worker.
CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:8080", "--timeout", "120", "app:app"]
