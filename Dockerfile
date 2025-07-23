FROM python:3.11-slim

# Install build tools for tgcrypto (if needed)
RUN apt-get update && apt-get install -y --no-install-recommends build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy bot code
COPY gifts_sniper.py . 

# Copy requirements (updated to use pyrofork)
COPY requirements.txt .

# Install dependencies: use PyroFork instead of Pyrogram
RUN pip install --no-cache-dir -r requirements.txt

# Create data directory for persistence (if needed)
RUN mkdir -p /app/data

CMD ["python", "gifts_sniper.py"]
