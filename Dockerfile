# ============================================================
# GiftSniper Dockerfile
#—
#   • Base:  python 3.11‑slim
#   • Workdir: /app          (как в прошлом образе)
#   • Никаких .env внутрь   (переменные задаёте при docker run)
# ============================================================

# 1. Базовый образ
FROM python:3.11-slim

# 2. Рабочая директория
WORKDIR /app                # → все COPY идут относительно /app

# 3. Системные пакеты (для tgcrypto / wheel‑сборки)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc && \
    rm -rf /var/lib/apt/lists/*

# 4. Зависимости
COPY requirements.txt /app          # тот же приём, что в старом Dockerfile
RUN pip install --no-cache-dir -r requirements.txt

# 5. Код бота
COPY gifts_sniper.py /app           # если рядом есть другие *.py — копируйте аналогично
#  └─ Не копируем .env — окружение задаётся снаружи

# 6. Папка для persistent‑данных (gifts.json)
RUN mkdir -p /app/data

# 7. Запуск
CMD ["python", "gifts_sniper.py"]