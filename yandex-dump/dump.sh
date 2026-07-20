#!/usr/bin/env bash
# dump.sh — дружелюбная обёртка над yandex_dump.py.
# Проверяет окружение, подсказывает на каждом шаге, ничего не делает
# «магически»: перед реальной выгрузкой всегда просит подтверждения.
set -euo pipefail
cd "$(dirname "$0")"

COOKIES_FILE="${1:-cookies.txt}"
OUT_DIR="${2:-}"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!!\033[0m %s\n' "$*"; }
err()   { printf '\033[31mОшибка:\033[0m %s\n' "$*" >&2; }

sanitize_dirname() {
    # оставляем безопасные ASCII-символы, остальное (в т.ч. любые
    # multibyte-символы) -> "_". Адреса почты по стандарту ASCII, так что
    # это ничего не теряет; tr -c с кириллицей в bracket-классе ведёт себя
    # по-разному в GNU/BSD, поэтому сознательно не полагаемся на локаль.
    printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '_' | sed 's/_\{2,\}/_/g; s/^_//; s/_$//'
}

# ---------------------------------------------------------------- python ---
PYTHON=""
for cand in python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then PYTHON="$cand"; break; fi
done
if [ -z "$PYTHON" ]; then
    err "python3 не найден. Установите Python 3.8+ и запустите скрипт снова."
    exit 1
fi

# Единственная зависимость — requests. Если её нет в текущем питоне,
# держим её в локальном .venv рядом со скриптом: так не трогаем системное
# окружение и не спотыкаемся о PIP_REQUIRE_VIRTUALENV или
# externally-managed-environment (PEP 668).
if [ -x ".venv/bin/python" ]; then
    PYTHON=".venv/bin/python"
fi
if ! "$PYTHON" -c "import requests" >/dev/null 2>&1; then
    if [ ! -x ".venv/bin/python" ]; then
        info "Пакет 'requests' не найден — создаю виртуальное окружение .venv..."
        if ! "$PYTHON" -m venv .venv; then
            err "Не удалось создать виртуальное окружение (.venv)."
            err "Создайте его вручную и поставьте зависимость:"
            err "  $PYTHON -m venv .venv && .venv/bin/pip install requests"
            exit 1
        fi
    fi
    PYTHON=".venv/bin/python"
    info "Ставлю requests в .venv..."
    if ! "$PYTHON" -m pip install --quiet --disable-pip-version-check requests; then
        err "Не удалось установить requests. Поставьте вручную:"
        err "  .venv/bin/pip install requests"
        exit 1
    fi
fi

# ---------------------------------------------------------------- cookies ---
if [ ! -f "$COOKIES_FILE" ]; then
    bold "Файл с cookies не найден: $COOKIES_FILE"
    cat <<'EOF'

Как его получить (это нужно сделать вручную — скрипт специально не
логинится в Яндекс сам, чтобы никогда не видеть ваш пароль):

  1. Откройте https://mail.yandex.ru в браузере и залогиньтесь в тот
     ящик, который хотите выгрузить.
  2. Откройте инструменты разработчика (F12 или Cmd+Opt+I) -> вкладка
     Network.
  3. Кликните на любое письмо в списке — в Network появятся запросы.
  4. Кликните на любой запрос к mail.yandex.ru -> вкладка Headers ->
     найдите заголовок "Cookie" в Request Headers -> скопируйте его
     ЗНАЧЕНИЕ целиком (это одна длинная строка вида
     "Session_id=...; yandexuid=...; ...").
  5. Вставьте эту строку в файл cookies.txt рядом со скриптом:

       printf '%s' 'Session_id=...; yandexuid=...; ...' > cookies.txt

     (Файл cookies.txt из расширений-экспортёров куки в формате
     Netscape тоже поддерживается — можно использовать его как есть.)

Куда уходят эти cookies: только на mail.yandex.ru. Скрипт открытый —
можно убедиться самостоятельно, там всего одна константа BASE.
Никогда не публикуйте этот файл и не отправляйте его никому: по этим
cookies можно зайти в ваш ящик без пароля. После выгрузки файл можно и
нужно удалить.

EOF
    exit 1
fi

# --------------------------------------------------------------- out dir ---
if [ -z "$OUT_DIR" ]; then
    read -r -p "Адрес почтового ящика, который выгружаем (например, box@example.com): " MAILBOX
    if [ -n "$MAILBOX" ]; then
        OUT_DIR="dump_$(sanitize_dirname "$MAILBOX")"
    else
        OUT_DIR="dump"
    fi
    info "Каталог для выгрузки: $OUT_DIR  (можно было задать явно вторым аргументом: ./dump.sh cookies.txt мой_каталог)"
fi

# ---------------------------------------------------------------- run ------
info "Проверяю список папок в ящике (ничего не скачивается)..."
"$PYTHON" yandex_dump.py --cookies "$COOKIES_FILE" --out "$OUT_DIR" --list-folders

echo
info "Пробую скачать одно письмо, чтобы убедиться, что всё работает..."
if ! "$PYTHON" yandex_dump.py --cookies "$COOKIES_FILE" --out "$OUT_DIR" --probe; then
    warn "Пробное скачивание не удалось — см. подсказки выше в выводе."
    warn "Обычно помогает --api-model или --source-template, см. README.md."
    exit 1
fi

echo
bold "Пробное скачивание прошло успешно."
read -r -p "Запустить полную выгрузку в '$OUT_DIR'? [y/N] " ans
case "$ans" in
    [yY]|[yY][eE][sS])
        info "Запускаю полную выгрузку. Это может занять от десятков минут до"
        info "нескольких часов в зависимости от размера ящика. Скрипт можно"
        info "прервать (Ctrl+C) и запустить снова — он продолжит с места"
        info "остановки."
        "$PYTHON" yandex_dump.py --cookies "$COOKIES_FILE" --out "$OUT_DIR" "${@:3}"
        echo
        bold "Готово. Письма лежат в: $OUT_DIR"
        ;;
    *)
        info "Хорошо, ничего не запускаю. Для ручного запуска:"
        echo "  $PYTHON yandex_dump.py --cookies $COOKIES_FILE --out $OUT_DIR"
        ;;
esac
