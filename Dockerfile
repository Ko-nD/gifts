FROM python:3.11-slim

# системные пакеты (без git!)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyrofork.zip .           # ← кладём архив внутрь образа
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gifts_sniper.py .
RUN mkdir -p /app/data
CMD ["python", "gifts_sniper.py"]
