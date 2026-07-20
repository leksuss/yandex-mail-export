#!/usr/bin/env bash
# upload.sh — дружелюбная обёртка над imap_upload.py.
# Только стандартная библиотека Python, ничего ставить не нужно.
set -euo pipefail
cd "$(dirname "$0")"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
err()   { printf '\033[31mОшибка:\033[0m %s\n' "$*" >&2; }

PYTHON=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
done
if [ -z "$PYTHON" ]; then
    err "python3 не найден. Установите Python 3.8+ и запустите скрипт снова."
    exit 1
fi

DUMP_DIR="${1:-}"
IMAP_USER="${2:-}"

if [ -z "$DUMP_DIR" ]; then
    read -r -p "Путь к каталогу с выгрузкой (из yandex-dump, содержит manifest.jsonl): " DUMP_DIR
fi
if [ ! -f "$DUMP_DIR/manifest.jsonl" ]; then
    err "Не нашёл $DUMP_DIR/manifest.jsonl — сначала выполните выгрузку скриптом yandex-dump/dump.sh"
    exit 1
fi

if [ -z "$IMAP_USER" ]; then
    read -r -p "Логин почтового ящика на новом сервере (например, box@example.com): " IMAP_USER
fi

HOST="${IMAP_HOST:-}"
if [ -z "$HOST" ]; then
    read -r -p "IMAP-сервер назначения (например, imap.beget.com — см. в панели вашего хостинга): " HOST
fi
if [ -z "$HOST" ]; then
    err "IMAP-сервер не указан."
    exit 1
fi
PORT="${IMAP_PORT:-993}"
info "IMAP-сервер: $HOST:$PORT (можно задать заранее: переменные окружения IMAP_HOST / IMAP_PORT)"

echo
info "Сначала dry-run — покажу, что и куда поедет, БЕЗ реальной заливки."
"$PYTHON" imap_upload.py --dump "$DUMP_DIR" --user "$IMAP_USER" \
    --host "$HOST" --port "$PORT" --dry-run "${@:3}"

echo
read -r -p "Пароль от ящика уже введёте в защищённом виде на следующем шаге. Продолжить заливку? [y/N] " ans
case "$ans" in
    [yY]|[yY][eE][sS])
        read -r -p "Проверить и пропустить письма, которые уже есть в ящике "\
"назначения (сверка по Message-ID, дольше на старте)? [y/N] " dd
        dedupe_flag=()
        case "$dd" in [yY]|[yY][eE][sS]) dedupe_flag=(--dedupe) ;; esac

        info "Запускаю заливку. Пароль спросится отдельно и никуда, кроме"
        info "самого IMAP-сервера, не отправляется."
        "$PYTHON" imap_upload.py --dump "$DUMP_DIR" --user "$IMAP_USER" \
            --host "$HOST" --port "$PORT" "${dedupe_flag[@]}" "${@:3}"
        echo
        bold "Готово."
        ;;
    *)
        info "Хорошо, ничего не запускаю."
        ;;
esac
