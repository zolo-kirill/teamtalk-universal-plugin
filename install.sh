#!/usr/bin/env bash
# Интерактивный установщик TeamTalk Universal Plugin.
# Меню: установка / удаление / выход. Запрашивает все параметры,
# клонирует репозиторий, ставит зависимости (apt, venv, SDK),
# создаёт .secrets/.env, настраивает systemd-сервис и запускает бота.
#
# Автор: Kirill. Вопросы — @zolo-kirill в Telegram.
#
# Запуск: bash install.sh
# Тестовый режим (без реальных действий): SETUP_DRY_RUN=1 bash install.sh
set -euo pipefail

CREATOR_LINE="Автор: Kirill (@zolo-kirill)"
REPO_URL_DEFAULT="https://github.com/zolo-kirill/teamtalk-universal-plugin.git"
UNIT_NAME="teamtalk-universal-plugin.service"
UNIT_DIR="$HOME/.config/systemd/user"
DRY_RUN="${SETUP_DRY_RUN:-0}"

say() { printf '%s\n' "$*"; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        say "    [проверка] $*"
        return 0
    fi
    "$@"
}

# ask VARNAME "Текст вопроса" [значение по умолчанию] — читает строку в переменную.
ask() {
    local var="$1" prompt="$2" default="${3:-}" val
    if [ -n "$default" ]; then
        read -r -p "$prompt [$default]: " val
        [ -z "$val" ] && val="$default"
    else
        read -r -p "$prompt: " val
    fi
    printf -v "$var" '%s' "$val"
}

ask_secret() {
    local var="$1" prompt="$2" val
    read -r -s -p "$prompt: " val
    say ""
    printf -v "$var" '%s' "$val"
}

ask_yn() {
    # ask_yn VARNAME "Текст вопроса" [по умолчанию y|n]
    local var="$1" prompt="$2" default="${3:-n}" ans
    while :; do
        read -r -p "$prompt (y/n) [$default]: " ans
        [ -z "$ans" ] && ans="$default"
        case "$ans" in
            y|Y|yes) printf -v "$var" 'y'; return 0 ;;
            n|N|no)  printf -v "$var" 'n'; return 0 ;;
            *) say "   Ответьте y или n." ;;
        esac
    done
}

# q ЗНАЧЕНИЕ — безопасные одинарные кавычки для shell: '$', '`', '\', пробелы.
q() {
    local v="$1" out="" c
    while [ -n "$v" ]; do
        c="${v:0:1}"
        if [ "$c" = "'" ]; then
            out="${out}'\\''"
        else
            out="${out}${c}"
        fi
        v="${v:1}"
    done
    printf "'%s'" "$out"
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$(id -u)" -eq 0 ]; then
    say "!! Вы запустили установщик от root. Он настраивает пользовательский сервис."
    say "   Выйдите из root и запустите как обычный пользователь:"
    say "   sudo bash install.sh"
    exit 1
fi

# ---------------------------------------------------------------- установка
do_install() {
    local REPO_DIR="$SCRIPT_DIR" SECRETS_DIR="" git_url="$REPO_URL_DEFAULT"
    local TEAMTALK_HOST="" TEAMTALK_TCP_PORT="" TEAMTALK_UDP_PORT=""
    local TEAMTALK_USERNAME="" TEAMTALK_PASSWORD="" TEAMTALK_NICKNAME=""
    local TEAMTALK_CHANNEL="" TG_TOKEN="" TG_OWNER_USER_ID="" TG_NOTIFY_CHAT_ID=""
    local YT_COOKIES="" RT_COOKIES="" YM_TOKEN_VAL=""
    local CONFIRM="" START_NOW=""

    say ""
    say "== Установка TeamTalk Universal Plugin =="

    # --- 1. репозиторий (клонируем, если рядом с установщиком нет бота) ---
    if [ ! -f "$REPO_DIR/bot.py" ]; then
        say "  Рядом с установщиком нет bot.py — клонирую репозиторий."
        ask git_url "Адрес репозитория" "$REPO_URL_DEFAULT"
        ask REPO_DIR "Куда клонировать?" "$HOME/teamtalk-universal-plugin"
        REPO_DIR="${REPO_DIR/#\~/$HOME}"
        if [ -e "$REPO_DIR" ]; then
            if [ -f "$REPO_DIR/bot.py" ]; then
                say "  В $REPO_DIR уже есть бот — использую как есть."
            else
                ask_yn REPLACE "Папка $REPO_DIR существует, но бота в ней нет. Очистить и клонировать заново?" n
                if [ "$REPLACE" != "y" ]; then
                    say "  Установка отменена."
                    return 1
                fi
                run rm -rf "$REPO_DIR"
                run git clone "$git_url" "$REPO_DIR"
            fi
        else
            run git clone "$git_url" "$REPO_DIR"
        fi
    fi
    REPO_DIR="$(cd "$REPO_DIR" && pwd)"
    SECRETS_DIR="$(cd "$REPO_DIR/.." && pwd)/.secrets"

    # --- 2. вопросы (адрес, логин и пароль обязательны) ---
    say ""
    say "  Заполните параметры TeamTalk:"
    while :; do
        ask TEAMTALK_HOST "Адрес сервера TeamTalk" ""
        ask TEAMTALK_TCP_PORT "TCP-порт" "10333"
        ask TEAMTALK_UDP_PORT "UDP-порт" "10333"
        ask TEAMTALK_USERNAME "Логин" ""
        ask_secret TEAMTALK_PASSWORD "Пароль"
        if [ -n "$TEAMTALK_HOST" ] && [ -n "$TEAMTALK_USERNAME" ] && [ -n "$TEAMTALK_PASSWORD" ]; then
            break
        fi
        say "  !! Адрес сервера, логин и пароль обязательны. Попробуйте ещё раз."
    done
    ask TEAMTALK_NICKNAME "Имя бота (ник)" "MusicBot"
    ask TEAMTALK_CHANNEL "Канал (пусто = корневой)" ""
    ask YM_TOKEN_VAL "OAuth-токен Яндекс.Музыки (пусто = пропустить)" ""
    ask TG_TOKEN "Токен Telegram-бота для реле (пусто = пропустить)" ""
    ask TG_OWNER_USER_ID "Ваш Telegram ID — владелец, пусто = пропустить" ""
    ask TG_NOTIFY_CHAT_ID "ID чата для уведомлений о входе/выходе, пусто = пропустить" ""
    ask YT_COOKIES "Путь к cookies.txt для YouTube (пусто = пропустить)" ""
    ask RT_COOKIES "Путь к rutube_cookies.txt (пусто = пропустить)" ""

    say ""
    say "  Проверьте введённые данные:"
    say "    Сервер:    $TEAMTALK_HOST:$TEAMTALK_TCP_PORT (udp $TEAMTALK_UDP_PORT)"
    say "    Логин:     $TEAMTALK_USERNAME  (ник: $TEAMTALK_NICKNAME)"
    if [ -n "$TEAMTALK_CHANNEL" ]; then
        say "    Канал:     $TEAMTALK_CHANNEL"
    else
        say "    Канал:     (корневой)"
    fi
    say "    Пароль:    ***"
    if [ -n "$TG_OWNER_USER_ID" ]; then
        say "    Владелец TG:   $TG_OWNER_USER_ID"
    fi
    if [ -n "$TG_NOTIFY_CHAT_ID" ]; then
        say "    Уведомл. TG:   $TG_NOTIFY_CHAT_ID"
    fi
    ask_yn CONFIRM "Всё верно? Продолжить установку" y
    if [ "$CONFIRM" != "y" ]; then
        say "  Установка отменена."
        return 1
    fi

    # --- 3. системные пакеты ---
    say ""
    say "== 3/6 Системные пакеты =="
    if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        say "  ffmpeg, python3, pip, venv..."
        run sudo apt-get update -y
        run sudo apt-get install -y ffmpeg python3 python3-pip python3-venv
    else
        say "  sudo/apt-get не найдены — пропускаю (пакеты нужно установить заранее)."
    fi

    # --- 4. python-окружение ---
    say ""
    say "== 4/6 Окружение Python (.venv) =="
    if [ -x "$REPO_DIR/.venv/bin/python" ]; then
        say "  .venv уже есть — обновляю зависимости."
    else
        run python3 -m venv "$REPO_DIR/.venv"
    fi
    run "$REPO_DIR/.venv/bin/python" -m pip install --upgrade pip
    run "$REPO_DIR/.venv/bin/python" -m pip install --upgrade yt-dlp yandex-music

    # --- 5. SDK ---
    say ""
    say "== 5/6 TeamTalk SDK =="
    local SDK_LIB=""
    SDK_LIB="$(ls -d "$REPO_DIR"/sdk/tt5sdk*/Library/TeamTalk_DLL/libTeamTalk5.so 2>/dev/null | head -1 || true)"
    if [ -n "$SDK_LIB" ]; then
        say "  SDK найден: $SDK_LIB"
    else
        say "  !! SDK не найден (sdk/tt5sdk*/Library/TeamTalk_DLL/libTeamTalk5.so)."
        say "     Проверьте, что репозиторий склонирован полностью. Без SDK бот не запустится."
    fi

    # --- 6. секреты и запуск ---
    say ""
    say "== 6/6 Секреты и запуск =="
    mkdir -p "$SECRETS_DIR"
    local ENV_FILE="$SECRETS_DIR/.env"
    if [ -f "$ENV_FILE" ]; then
        local BAK="$ENV_FILE.bak.$(date +%s)"
        run cp "$ENV_FILE" "$BAK"
        say "  Старый .env сохранён: $BAK"
    fi
    if [ "$DRY_RUN" = "1" ]; then
        say "    [проверка] записываю $ENV_FILE"
    else
        {
            say "# TeamTalk Universal Plugin — конфигурация (создан $(date -Iseconds))"
            say "TEAMTALK_HOST=$(q "$TEAMTALK_HOST")"
            say "TEAMTALK_TCP_PORT=$(q "$TEAMTALK_TCP_PORT")"
            say "TEAMTALK_UDP_PORT=$(q "$TEAMTALK_UDP_PORT")"
            say "TEAMTALK_USERNAME=$(q "$TEAMTALK_USERNAME")"
            say "TEAMTALK_PASSWORD=$(q "$TEAMTALK_PASSWORD")"
            say "TEAMTALK_NICKNAME=$(q "$TEAMTALK_NICKNAME")"
            if [ -n "$TEAMTALK_CHANNEL" ]; then
                say "TEAMTALK_CHANNEL=$(q "$TEAMTALK_CHANNEL")"
            fi
            if [ -n "$TG_TOKEN" ]; then
                say "TG_TOKEN=$(q "$TG_TOKEN")"
            fi
            if [ -n "$TG_OWNER_USER_ID" ]; then
                say "TG_OWNER_USER_ID=$(q "$TG_OWNER_USER_ID")"
            fi
            if [ -n "$TG_NOTIFY_CHAT_ID" ]; then
                say "TG_NOTIFY_CHAT_ID=$(q "$TG_NOTIFY_CHAT_ID")"
            fi
        } > "$ENV_FILE"
        say "  Конфиг записан: $ENV_FILE"
    fi

    if [ -n "$YT_COOKIES" ]; then
        if [ -f "$YT_COOKIES" ]; then
            run cp "$YT_COOKIES" "$SECRETS_DIR/cookies.txt"
            say "  cookies.txt (YouTube) скопирован в .secrets/"
        else
            say "  !! Файл не найден: $YT_COOKIES — куки YouTube пропущены."
        fi
    fi
    if [ -n "$RT_COOKIES" ]; then
        if [ -f "$RT_COOKIES" ]; then
            run cp "$RT_COOKIES" "$SECRETS_DIR/rutube_cookies.txt"
            say "  rutube_cookies.txt скопирован в .secrets/"
        else
            say "  !! Файл не найден: $RT_COOKIES — куки Rutube пропущены."
        fi
    fi
    if [ -n "$YM_TOKEN_VAL" ]; then
        run sh -c 'printf "%s" "$1" > "$2"' _ "$YM_TOKEN_VAL" "$SECRETS_DIR/ym_token.txt"
        say "  OAuth-токен Яндекс.Музыки записан в .secrets/ym_token.txt"
    fi

    # systemd-сервис (если есть) или запуск в фоне
    if [ -d "/run/systemd/system" ] && [ -f "$REPO_DIR/teamtalk-universal-plugin.service" ]; then
        mkdir -p "$UNIT_DIR"
        if [ "$DRY_RUN" = "1" ]; then
            say "    [проверка] sed __DIR__ -> $UNIT_DIR/$UNIT_NAME"
        else
            sed -e "s|__DIR__|$REPO_DIR|g" "$REPO_DIR/teamtalk-universal-plugin.service" > "$UNIT_DIR/$UNIT_NAME"
        fi
        run systemctl --user daemon-reload
        run systemctl --user enable "$UNIT_NAME"
        say "  Запускаю бота..."
        run systemctl --user start "$UNIT_NAME"
        if command -v loginctl >/dev/null 2>&1; then
            run sudo loginctl enable-linger "$(id -un)" || say "  (не удалось включить linger — после перезагрузки сервис запустится только после входа в систему)"
        fi
    else
        say "  systemd не найден — автозапуск пропущен."
        ask_yn START_NOW "Запустить бота сейчас (в фоне)?" y
        if [ "$START_NOW" = "y" ]; then
            run bash -c "cd \"$REPO_DIR\" && nohup bash run.sh > bot.log 2>&1 &"
            say "  Бот запущен в фоне. Лог: $REPO_DIR/bot.log"
        else
            say "  Ручной запуск: bash $REPO_DIR/run.sh"
        fi
    fi

    say ""
    say "== Установка завершена =="
    say "  Папка:    $REPO_DIR"
    say "  Секреты:  $SECRETS_DIR/.env"
    if [ -d "/run/systemd/system" ]; then
        say "  Статус:   systemctl --user status $UNIT_NAME"
        say "  Логи:     journalctl --user -u $UNIT_NAME -f"
    fi
    say "  $CREATOR_LINE"
}

# --------------------------------------------------------------- удаление
do_uninstall() {
    local repo="" secrets="" unit="$UNIT_DIR/$UNIT_NAME"

    say ""
    say "== Удаление TeamTalk Universal Plugin =="

    # находим репозиторий: рядом с установщиком или из ExecStart сервиса
    if [ -f "$SCRIPT_DIR/bot.py" ]; then
        repo="$SCRIPT_DIR"
    elif [ -f "$unit" ]; then
        repo="$(sed -n 's|^ExecStart=/bin/bash \(.*\)/run\.sh$|\1|p' "$unit" | head -1 || true)"
    fi
    if [ -n "$repo" ]; then
        repo="$(cd "$repo" 2>/dev/null && pwd || echo "")"
    fi

    if [ -z "$repo" ] && [ ! -f "$unit" ]; then
        say "  Не удалось найти установленного бота: нет ни сервиса $unit, ни bot.py рядом."
        say "  Удалять нечего."
        return 0
    fi

    # останавливаем и удаляем сервис
    if [ -f "$unit" ]; then
        if [ -d "/run/systemd/system" ]; then
            say "  Останавливаю сервис..."
            run systemctl --user stop "$UNIT_NAME" || true
            run systemctl --user disable "$UNIT_NAME" || true
        fi
        run rm -f "$unit"
        if [ -d "/run/systemd/system" ]; then
            run systemctl --user daemon-reload
        fi
        say "  Сервис остановлен и удалён."
    else
        say "  systemd-сервис не установлен (пропускаю)."
    fi

    # удаляем репозиторий (вместе с .venv)
    if [ -n "$repo" ]; then
        say ""
        say "  Репозиторий: $repo"
        ask_yn DEL_REPO "Удалить его (вместе с .venv)?" n
        if [ "$DEL_REPO" = "y" ]; then
            run rm -rf "$repo"
            say "  Репозиторий удалён."
            secrets="$(dirname "$repo")/.secrets"
        else
            say "  Репозиторий сохранён."
        fi
    fi

    # удаляем секреты
    if [ -n "$repo" ] && [ -d "$(dirname "$repo")/.secrets" ]; then
        secrets="$(dirname "$repo")/.secrets"
    fi
    if [ -n "$secrets" ] && [ -d "$secrets" ]; then
        say ""
        say "  Секреты: $secrets"
        ask_yn DEL_SECRETS "Удалить папку с секретами (пароль, токены)?" n
        if [ "$DEL_SECRETS" = "y" ]; then
            run rm -rf "$secrets"
            say "  Секреты удалены."
        fi
    fi

    say ""
    say "== Бот удалён. До свидания! =="
    say "  $CREATOR_LINE"
}

# --------------------------------------------------------------------- меню
menu() {
    while :; do
        say ""
        say "Что нужно сделать?"
        say "  1) Установить бота"
        say "  2) Удалить бота"
        say "  3) Выход"
        local choice
        read -r -p "Выберите (1-3): " choice
        case "$choice" in
            1) do_install ;;
            2) do_uninstall ;;
            3) say "До свидания!"; exit 0 ;;
            *) say "  Такого пункта нет — выберите 1, 2 или 3." ;;
        esac
    done
}

say "===================================================="
say "  TeamTalk Universal Plugin — установка"
say "===================================================="
say ""
say "  Добро пожаловать в установку TeamTalk Universal Plugin!"
say ""
say "  Это музыкальный бот для голосовых каналов TeamTalk 5."
say "  Что он умеет:"
say "    • ищет и играет музыку по запросу — YouTube и Яндекс.Музыка;"
say "    • ставит в очередь плейлисты, радио и избранное;"
say "    • переключает треки, перематывает, меняет громкость;"
say "    • проигрывает в канале аудио и команды из Telegram-реле;"
say "    • сообщает в Telegram о входе и выходе пользователей."
say ""
say "  Автор: Kirill (@zolo-kirill)"
say ""
if [ "$DRY_RUN" = "1" ]; then
    say "  (режим проверки SETUP_DRY_RUN=1 — реальных действий не выполняется)"
fi
menu
