#!/usr/bin/env bash
# Компонент: конфигуратор.
# Спрашивает данные TeamTalk и Telegram, пишет .secrets/.env, копирует куки
# YouTube/RuTube, сохраняет OAuth-токен Яндекс.Музыки, при необходимости
# поднимает обход блокировки Telegram.
#
#   bash components/config.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$HERE/../lib/common.sh"
# shellcheck source=../lib/tgproxy.sh
. "$HERE/../lib/tgproxy.sh"

require_non_root
ensure_repo || exit 1

TEAMTALK_HOST=""
TEAMTALK_TCP_PORT=""
TEAMTALK_UDP_PORT=""
TEAMTALK_USERNAME=""
TEAMTALK_PASSWORD=""
TEAMTALK_NICKNAME=""
TEAMTALK_CHANNEL=""
TG_TOKEN=""
TG_OWNER_USER_ID=""
TG_NOTIFY_CHAT_ID=""
TG_MUSIC_TOKEN=""
TG_PROXY_VAL=""
YT_COOKIES=""
RT_COOKIES=""
YM_TOKEN_VAL=""
CONFIRM=""

say ""
say "== Конфигуратор TeamTalk Universal Plugin =="
say "  Бот:      $REPO_DIR"
say "  Секреты:  $SECRETS_DIR/.env"

# ---- 1. TeamTalk ----
say ""
say "  Параметры TeamTalk (адрес, логин и пароль обязательны):"
while :; do
    ask TEAMTALK_HOST "  Адрес сервера TeamTalk" ""
    ask TEAMTALK_TCP_PORT "  TCP-порт" "10333"
    ask TEAMTALK_UDP_PORT "  UDP-порт" "10333"
    ask TEAMTALK_USERNAME "  Логин" ""
    ask_secret TEAMTALK_PASSWORD "  Пароль"
    if [ -n "$TEAMTALK_HOST" ] && [ -n "$TEAMTALK_USERNAME" ] && [ -n "$TEAMTALK_PASSWORD" ]; then
        break
    fi
    say "  !! Адрес сервера, логин и пароль обязательны. Попробуйте ещё раз."
done
ask TEAMTALK_NICKNAME "  Имя бота (ник)" "UniversalBot"
ask TEAMTALK_CHANNEL "  Канал (пусто = корневой)" ""

# ---- 2. Telegram ----
say ""
say "  Telegram (необязательно):"
ask TG_TOKEN "  Токен бота для реле-команд (пусто = пропустить)" ""
if [ -n "$TG_TOKEN" ]; then
    ask TG_OWNER_USER_ID "  Ваш Telegram ID — владелец (пусто = пропустить)" ""
    ask TG_NOTIFY_CHAT_ID "  ID чата для уведомлений о входе/выходе (пусто = пропустить)" ""
    ask TG_MUSIC_TOKEN "  Токен бота для музыки (пусто = пропустить)" ""
fi

# ---- 3. Медиа ----
say ""
ask YM_TOKEN_VAL "  OAuth-токен Яндекс.Музыки (пусто = пропустить)" ""
ask YT_COOKIES "  Путь к cookies.txt для YouTube (пусто = пропустить)" ""
ask RT_COOKIES "  Путь к rutube_cookies.txt (пусто = пропустить)" ""

# ---- 4. подтверждение ----
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
[ -n "$TG_OWNER_USER_ID" ] && say "    Владелец TG:   $TG_OWNER_USER_ID"
[ -n "$TG_NOTIFY_CHAT_ID" ] && say "    Уведомл. TG:   $TG_NOTIFY_CHAT_ID"
say "    Telegram:  $([ -n "$TG_TOKEN" ] && echo 'включён' || echo 'не настроен')"
say "    Яндек.Музыка: $([ -n "$YM_TOKEN_VAL" ] && echo 'токен есть' || echo 'нет токена')"
ask_yn CONFIRM "  Всё верно? Записать конфигурацию" y
if [ "$CONFIRM" != "y" ]; then
    say "  Отменено, ничего не записано."
    exit 1
fi

# ---- 5. .env ----
mkdir -p "$SECRETS_DIR"
ENV_FILE="$SECRETS_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    BAK="$ENV_FILE.bak.$(date +%s)"
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
        [ -n "$TEAMTALK_CHANNEL" ]  && say "TEAMTALK_CHANNEL=$(q "$TEAMTALK_CHANNEL")"
        [ -n "$TG_TOKEN" ]          && say "TG_TOKEN=$(q "$TG_TOKEN")"
        [ -n "$TG_OWNER_USER_ID" ]  && say "TG_OWNER_USER_ID=$(q "$TG_OWNER_USER_ID")"
        [ -n "$TG_NOTIFY_CHAT_ID" ] && say "TG_NOTIFY_CHAT_ID=$(q "$TG_NOTIFY_CHAT_ID")"
        [ -n "$TG_MUSIC_TOKEN" ]    && say "TG_MUSIC_TOKEN=$(q "$TG_MUSIC_TOKEN")"
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE" 2>/dev/null || true
    say "  Конфигурация записана: $ENV_FILE"
fi

# ---- 6. файлы ----
if [ -n "$YT_COOKIES" ]; then
    if [ -f "$YT_COOKIES" ]; then
        run cp "$YT_COOKIES" "$SECRETS_DIR/cookies.txt"
        say "  cookies.txt (YouTube) скопирован в $SECRETS_DIR"
    else
        say "  !! Файл не найден: $YT_COOKIES — куки YouTube пропущены."
    fi
fi
if [ -n "$RT_COOKIES" ]; then
    if [ -f "$RT_COOKIES" ]; then
        run cp "$RT_COOKIES" "$SECRETS_DIR/rutube_cookies.txt"
        say "  rutube_cookies.txt скопирован в $SECRETS_DIR"
    else
        say "  !! Файл не найден: $RT_COOKIES — куки RuTube пропущены."
    fi
fi
if [ -n "$YM_TOKEN_VAL" ]; then
    run sh -c 'printf "%s" "$1" > "$2"' _ "$YM_TOKEN_VAL" "$SECRETS_DIR/ym_token.txt"
    say "  OAuth-токен Яндекс.Музыки записан в $SECRETS_DIR/ym_token.txt"
fi

# ---- 7. обход блокировки Telegram ----
say ""
say "== Обход блокировки Telegram =="
if [ -n "$TG_TOKEN" ]; then
    setup_telegram_proxy TG_PROXY_VAL
    if [ -n "$TG_PROXY_VAL" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            say "    [проверка] добавлю TG_PROXY=$TG_PROXY_VAL в $ENV_FILE"
        elif grep -q '^TG_PROXY=' "$ENV_FILE" 2>/dev/null; then
            sed -i "s|^TG_PROXY=.*|TG_PROXY=$(q "$TG_PROXY_VAL")|" "$ENV_FILE"
            say "  TG_PROXY обновлён в $ENV_FILE"
        else
            say "TG_PROXY=$(q "$TG_PROXY_VAL")" >> "$ENV_FILE"
            say "  TG_PROXY добавлен в $ENV_FILE"
        fi
    fi
else
    say "  Токен Telegram-бота не задан — обход не требуется."
fi

say ""
say "== Конфигурация готова =="
say "  Файл:    $ENV_FILE"
say "  Проверить запуск: bash $REPO_DIR/run.sh --show"
say "  $CREATOR_LINE"
