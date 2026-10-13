FROM python:3.12-slim

# Встановлюємо необхідні змінні оточення
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Kyiv

WORKDIR /app

# Встановлюємо системні залежності при необхідності
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Копіюємо файл залежностей та встановлюємо їх
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium --with-deps

# Копіюємо вихідний код проекту
COPY . .

# Створюємо директорію для збереження бази даних
RUN mkdir -p /app/data

# Запуск бота
CMD ["python", "bot.py"]
