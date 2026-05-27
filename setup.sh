#!/bin/bash
# ═══════════════════════════════════════════════════════
#   Telegram Reminder Bot — установщик
#   Использование:
#   curl -sSL https://raw.githubusercontent.com/wellbob/reminder-bot/main/setup.sh | bash
# ═══════════════════════════════════════════════════════

set -e

REPO="https://github.com/wellbob/reminder-bot.git"
INSTALL_DIR="/opt/reminder-bot"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║      🔔 Telegram Reminder Bot            ║"
echo "║         Автоматическая установка         ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Проверка зависимостей ────────────────────────────────
echo "🔍 Проверяю зависимости..."

if ! command -v docker &>/dev/null; then
    echo -e "${RED}❌ Docker не найден.${NC}"
    echo "   Установи Docker: https://docs.docker.com/get-docker/"
    exit 1
fi

if ! docker compose version &>/dev/null 2>&1; then
    echo -e "${RED}❌ Docker Compose не найден.${NC}"
    exit 1
fi

if ! command -v git &>/dev/null; then
    echo "📦 Устанавливаю git..."
    apt-get update -qq && apt-get install -y -qq git
fi

echo -e "${GREEN}✅ Все зависимости найдены.${NC}"
echo ""

# ── Ввод токена ──────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Шаг 1 из 2 — Токен бота"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Создай бота у @BotFather в Telegram:"
echo "  /newbot → следуй инструкциям → скопируй токен"
echo ""
read -rp "  Вставь токен бота: " BOT_TOKEN

if [[ -z "$BOT_TOKEN" ]]; then
    echo -e "${RED}❌ Токен не введён. Установка прервана.${NC}"
    exit 1
fi

if ! echo "$BOT_TOKEN" | grep -qE '^[0-9]+:[A-Za-z0-9_-]+$'; then
    echo -e "${RED}❌ Токен выглядит неверно. Проверь и попробуй снова.${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Токен принят.${NC}"
echo ""

# ── Установка ────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Шаг 2 из 2 — Установка"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📥 Скачиваю файлы бота..."

if [ -d "$INSTALL_DIR" ]; then
    echo -e "${YELLOW}⚠️  Папка $INSTALL_DIR уже существует — обновляю...${NC}"
    cd "$INSTALL_DIR"
    git pull --quiet
else
    git clone --quiet "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

echo "BOT_TOKEN=${BOT_TOKEN}" > .env
mkdir -p data

echo ""
echo "🔨 Собираю Docker образ (1-2 минуты)..."
docker compose build --quiet

echo ""
echo "🚀 Запускаю бота..."
docker compose up -d

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   ✅  Бот успешно установлен!            ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  📁 Папка:  $INSTALL_DIR"
echo ""
echo "  Полезные команды:"
echo "  ┌──────────────────────────────────────────────────────"
echo "  │ Логи:      cd $INSTALL_DIR && docker compose logs -f"
echo "  │ Стоп:      docker compose stop reminder-bot"
echo "  │ Обновить:  git pull && docker compose build && docker compose up -d"
echo "  └──────────────────────────────────────────────────────"
echo ""
echo -e "${GREEN}  Напиши боту /start в Telegram!${NC}"
echo ""
