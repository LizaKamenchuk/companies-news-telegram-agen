# Python 3.12, лёгкий образ
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Warsaw

# Часовой пояс (логам будет легче доверять)
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Ставим зависимости отдельно — лучше кэшируется
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Код бота
COPY bot.py .

# Нерутовый пользователь
RUN useradd -m appuser && chown -R appuser /app
USER appuser

# Никаких портов не нужно — бот работает по long polling
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import aiohttp, aiogram; print('ok')"

CMD ["python", "bot.py"]
