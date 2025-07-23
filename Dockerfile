FROM python:3.11-slim

# для tgcrypto понадобятся build-tools
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# копируем весь wheelhouse и req
COPY wheelhouse ./wheelhouse
COPY requirements.txt .

# pip ставит всё, не обращаясь во внешний интернет
RUN pip install --no-cache-dir -r requirements.txt

# копируем сам бот
COPY gifts_sniper.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "gifts_sniper.py"]
