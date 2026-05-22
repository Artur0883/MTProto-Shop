#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

COMPOSE_CMD="docker compose"
if ! docker compose version >/dev/null 2>&1; then
  if docker-compose version >/dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
  fi
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

pause() {
  echo
  read -r -p "Нажмите Enter, чтобы вернуться в меню..." _
}

print_header() {
  clear || true
  echo "========================================"
  echo "        MTProto Shop — меню управления"
  echo "========================================"
  echo
}

need_env() {
  if [[ ! -f .env ]]; then
    echo -e "${YELLOW}.env не найден. Создаю из .env.example...${NC}"
    cp .env.example .env
    echo -e "${RED}Заполните .env: BOT_TOKEN, ADMIN_ID, SERVER_HOST, SUPPORT_CONTACT.${NC}"
    return 1
  fi
}

run_bot_cmd() {
  if $COMPOSE_CMD ps -q bot >/dev/null 2>&1 && [[ -n "$($COMPOSE_CMD ps -q bot 2>/dev/null || true)" ]]; then
    $COMPOSE_CMD exec -T bot python proxy_manager.py "$@"
  else
    $COMPOSE_CMD run --rm bot python proxy_manager.py "$@"
  fi
}

reload_proxy() {
  echo
  echo -e "${BLUE}Применяю изменения proxy...${NC}"
  if $COMPOSE_CMD kill -s SIGUSR2 mtproto >/dev/null 2>&1; then
    echo -e "${GREEN}Готово: mtproto перечитал config через SIGUSR2.${NC}"
  else
    echo -e "${YELLOW}SIGUSR2 не сработал, делаю restart mtproto...${NC}"
    $COMPOSE_CMD restart mtproto
  fi
}

install_or_update() {
  need_env || {
    echo
    echo "После заполнения .env снова запустите: ./manage.sh"
    return 0
  }
  echo -e "${BLUE}Собираю и запускаю контейнеры...${NC}"
  $COMPOSE_CMD up -d --build --force-recreate
  $COMPOSE_CMD ps
}

show_status() {
  $COMPOSE_CMD ps
}

show_bot_logs() {
  echo "Выход из логов: Ctrl+C"
  $COMPOSE_CMD logs -f --tail=120 bot
}

show_proxy_logs() {
  echo "Выход из логов: Ctrl+C"
  $COMPOSE_CMD logs -f --tail=120 mtproto
}

restart_bot() {
  echo
  echo -e "${BLUE}Пересоздаю Telegram-бота...${NC}"
  $COMPOSE_CMD up -d --build --force-recreate bot
  echo -e "${GREEN}Готово: бот пересоздан.${NC}"
  echo
  $COMPOSE_CMD ps bot
}

restart_proxy_container() {
  echo
  echo -e "${BLUE}Пересоздаю MTProto proxy...${NC}"
  $COMPOSE_CMD up -d --force-recreate mtproto
  echo -e "${GREEN}Готово: MTProto proxy пересоздан.${NC}"
  echo
  $COMPOSE_CMD ps mtproto
}

restart_all_services() {
  echo
  echo -e "${BLUE}Пересоздаю бота и MTProto proxy...${NC}"
  $COMPOSE_CMD up -d --build --force-recreate bot mtproto
  echo -e "${GREEN}Готово: бот и MTProto proxy пересозданы.${NC}"
  echo
  $COMPOSE_CMD ps
}

reboot_vps() {
  echo -e "${RED}Внимание: VPS будет полностью перезагружен.${NC}"
  read -r -p "Точно перезагрузить сервер? Напишите REBOOT: " answer
  [[ "$answer" == "REBOOT" ]] || { echo "Отменено"; return 0; }
  echo -e "${YELLOW}Перезагружаю сервер... SSH-сессия сейчас отключится.${NC}"
  reboot
}

list_keys() {
  run_bot_cmd list
}

add_key() {
  echo "Для клиента Telegram лучше использовать формат: tg_TELEGRAM_ID"
  read -r -p "Введите client_id: " client_id
  [[ -n "$client_id" ]] || { echo "client_id пустой"; return 1; }
  run_bot_cmd create "$client_id"
  reload_proxy
}

rotate_key_by_telegram_id() {
  read -r -p "Введите telegram_id клиента: " telegram_id
  [[ "$telegram_id" =~ ^[0-9]+$ ]] || { echo "telegram_id должен быть числом"; return 1; }
  run_bot_cmd rotate-telegram "$telegram_id"
  reload_proxy
}

show_link_by_telegram_id() {
  read -r -p "Введите telegram_id клиента: " telegram_id
  [[ "$telegram_id" =~ ^[0-9]+$ ]] || { echo "telegram_id должен быть числом"; return 1; }
  run_bot_cmd link "tg_${telegram_id}"
}

show_link_by_client_id() {
  read -r -p "Введите client_id: " client_id
  [[ -n "$client_id" ]] || { echo "client_id пустой"; return 1; }
  run_bot_cmd link "$client_id"
}

delete_key() {
  read -r -p "Введите client_id для удаления: " client_id
  [[ -n "$client_id" ]] || { echo "client_id пустой"; return 1; }
  read -r -p "Точно удалить ключ ${client_id}? Напишите YES: " answer
  [[ "$answer" == "YES" ]] || { echo "Отменено"; return 0; }
  run_bot_cmd delete "$client_id"
  reload_proxy
}

edit_env() {
  ${EDITOR:-nano} .env
}

backup_now() {
  mkdir -p backups
  archive="backups/mtproto-shop-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
  tar -czf "$archive" data proxy/config .env 2>/dev/null || true
  echo "Бэкап создан: $archive"
}

while true; do
  print_header
  echo "1) 🚀 Установка / обновление / запуск"
  echo "2) ✅ Статус контейнеров"
  echo "3) 📄 Логи бота"
  echo "4) 📄 Логи proxy"
  echo "5) 📋 Список ключей"
  echo "6) ➕ Добавить ключ вручную"
  echo "7) 🔄 Обновить ключ клиента по Telegram ID + синхронизировать SQLite"
  echo "8) 🔗 Показать ссылку по Telegram ID"
  echo "9) 🔗 Показать ссылку по client_id"
  echo "10) ❌ Удалить ключ по client_id"
  echo "11) ♻️ Применить изменения proxy"
  echo "12) ⚙️ Открыть .env"
  echo "13) 💾 Сделать бэкап"
  echo "14) 🔁 Пересоздать Telegram-бота"
  echo "15) 🔁 Пересоздать MTProto proxy"
  echo "16) 🔁 Пересоздать бота + MTProto proxy"
  echo "17) 🖥️ Перезагрузить VPS полностью"
  echo "0) 🚪 Выход"
  echo
  read -r -p "Выберите действие: " choice
  echo
  case "$choice" in
    1) install_or_update; pause ;;
    2) show_status; pause ;;
    3) show_bot_logs ;;
    4) show_proxy_logs ;;
    5) list_keys; pause ;;
    6) add_key; pause ;;
    7) rotate_key_by_telegram_id; pause ;;
    8) show_link_by_telegram_id; pause ;;
    9) show_link_by_client_id; pause ;;
    10) delete_key; pause ;;
    11) reload_proxy; pause ;;
    12) edit_env; pause ;;
    13) backup_now; pause ;;
    14) restart_bot; pause ;;
    15) restart_proxy_container; pause ;;
    16) restart_all_services; pause ;;
    17) reboot_vps ;;
    0) exit 0 ;;
    *) echo "Неверный пункт"; pause ;;
  esac
done
