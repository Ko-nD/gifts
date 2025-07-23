FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gifts_sniper.py .
RUN mkdir -p /app/data
CMD ["python", "gifts_sniper.py"]

docker run --rm giftsniper python - <<'PY'
import pyrogram, inspect
print("Pyrogram version:", pyrogram.__version__)
print("get_available_gifts exists:",
      hasattr(pyrogram.Client, "get_available_gifts"))
PY