FROM python:3.11-slim

# Системные зависимости (git нужен для установки пакетов с GitHub)
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем Tinkoff Invest SDK напрямую с GitHub (на PyPI недоступен)
RUN pip install --no-cache-dir "git+https://github.com/Tinkoff/invest-python.git"

# Копируем код
COPY . .

# Директория для SQLite БД (если используется)
RUN mkdir -p /app/data

# Запуск
CMD ["python", "main.py"]
