# ------------------------------------------------------------
# GiftSniper – минимальный образ (Python 3.11‑slim)
# ------------------------------------------------------------
FROM python:3.11-slim

# 1. Системные зависимости для tgcrypto
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# 2. Рабочая директория
WORKDIR /app

# 3. Python‑зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Исходники бота
COPY gifts_sniper.py .

# 5. Каталог под persistent‑файлы
RUN mkdir -p /app/data
ENV PYTHONUNBUFFERED=1

# 6. Некорневой пользователь (опционально)
RUN useradd -m runner
USER runner

# 7. Запуск
CMD ["python", "gifts_sniper.py"]
