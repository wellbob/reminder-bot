#!/bin/bash
# Скрипт быстрого развёртывания Telegram Reminder Bot

set -e

echo "========================================="
echo "  Telegram Reminder Bot — установка"
echo "========================================="

# Проверяем Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker не найден. Установи Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

if ! docker compose version &> /dev/null; then
    echo "❌ Docker Compose не найден."
    exit 1
fi

echo "✅ Docker найден: $(docker --version)"

# Создаём .env если не существует
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo ""
        echo "⚠️  Файл .env создан из шаблона."
        echo "    Открой .env и вставь свой BOT_TOKEN:"
        echo "    nano .env"
        echo ""
        read -p "Нажми Enter после того как вставил токен..."
    else
        echo "❌ Файл .env.example не найден."
        exit 1
    fi
fi

# Проверяем что токен задан
if grep -q "your_telegram_bot_token_here" .env; then
    echo "❌ Токен не задан! Открой .env и замени your_telegram_bot_token_here на реальный токен."
    exit 1
fi

# Создаём папку для данных
mkdir -p data

echo ""
echo "🔨 Собираем образ..."
docker compose build

echo ""
echo "🚀 Запускаем бота..."
docker compose up -d

echo ""
echo "========================================="
echo "  ✅ Бот запущен!"
echo "========================================="
echo ""
echo "Логи:       docker compose logs -f reminder-bot"
echo "Остановить: docker compose stop reminder-bot"
echo "Статус:     docker compose ps"
echo ""
