#!/usr/bin/env bash
# Интерактивный установщик TeamTalk Universal Plugin.
# Меню: установка / удаление / выход. Запрашивает все параметры,
# клонирует репозиторий, ставит зависимости (apt, venv, SDK),
# при необходимости поднимает обход блокировки Telegram из VPN-ключа,
# создаёт .secrets/.env, настраивает systemd-сервис и запускает бота.
#
# Автор: Kirill. Вопросы — @zolo-kirill в Telegram.
#
# Запуск: bash install.sh
# Тестовый режим (без реальных действий): SETUP_DRY_RUN=1 bash install.sh
set -euo pipefail

CREATOR_LINE="Автор: Kirill (@zolo-kirill)"
REPO_URL_DEFAULT="https://github.com/zolo-kirill/teamtalk-universal-plugin.git"
UNIT_NAME="tt-plugin.service"
UNIT_DIR="$HOME/.config/systemd/user"
XRAY_CONF="${XRAY_CONF:-/usr/local/etc/xray/config.json}"
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

# ------------------------------------------------------------- обход Telegram
# Telegram в России заблокирован, поэтому установщик сам проверяет доступность
# api.telegram.org и, если нужно, поднимает локальный прокси из VPN-ключа
# пользователя (VLESS, VMess или Trojan). Бот ходит в Telegram через
# 127.0.0.1:8118 (HTTP) — этот адрес попадает в .secrets/.env как TG_PROXY.

tg_reachable() {
    # 0 — api.telegram.org отвечает, 1 — не отвечает (или нет curl)
    command -v curl >/dev/null 2>&1 || return 1
    local code
    code="$(curl -s -m 8 -o /dev/null -w '%{http_code}' https://api.telegram.org 2>/dev/null || true)"
    [ -n "$code" ] && [ "$code" != "000" ]
}

proxy_works() {
    # proxy_works АДРЕС — проходит ли через прокси сам Telegram
    local p="$1" code
    case "$p" in
        socks5h://*|socks5://*)
            code="$(curl -s -m 15 --socks5-hostname "${p#*://}" -o /dev/null -w '%{http_code}' https://api.telegram.org 2>/dev/null || true)"
            ;;
        http://*|https://*)
            code="$(curl -s -m 15 -x "$p" -o /dev/null -w '%{http_code}' https://api.telegram.org 2>/dev/null || true)"
            ;;
        *)
            return 1
            ;;
    esac
    [ -n "$code" ] && [ "$code" != "000" ]
}

gen_xray_config() {
    # gen_xray_config "vless://..." ФАЙЛ — собирает конфиг Xray из ссылки-ключа.
    # Поддерживаются vless://, vmess://, trojan://. Печатает JSON в файл.
    python3 - "$1" > "$2" <<'PYGEN'
import sys, json, base64, urllib.parse as up


def die(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)


def split_hostport(hp, default_port):
    hp = hp.strip().rstrip("/")
    if hp.startswith("["):                      # [2001:db8::1]:443
        host, _, rest = hp[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port or default_port)
    if hp.count(":") == 1:
        host, port = hp.split(":")
        return host, int(port or default_port)
    return hp, int(default_port)


def build(url):
    if url.startswith("vless://"):
        p = up.urlparse(url)
        uuid, _, hp = p.netloc.rpartition("@")
        if not uuid:
            die("в ссылке нет UUID")
        host, port = split_hostport(hp, 443)
        q = up.parse_qs(p.query)
        g = lambda k, d=None: (q.get(k) or [d])[0]
        user = {"id": uuid, "encryption": g("encryption", "none") or "none"}
        if g("flow"):
            user["flow"] = g("flow")
        return {"tag": "proxy", "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                "streamSettings": {"network": g("type", "tcp") or "tcp",
                                   "security": g("security", "none") or "none"}}, host, g

    if url.startswith("trojan://"):
        p = up.urlparse(url)
        pw, _, hp = p.netloc.rpartition("@")
        if not pw:
            die("в ссылке нет пароля")
        host, port = split_hostport(hp, 443)
        q = up.parse_qs(p.query)
        g = lambda k, d=None: (q.get(k) or [d])[0]
        return {"tag": "proxy", "protocol": "trojan",
                "settings": {"servers": [{"address": host, "port": port,
                                          "password": up.unquote(pw)}]},
                "streamSettings": {"network": g("type", "tcp") or "tcp",
                                   "security": g("security", "tls") or "tls"}}, host, g

    if url.startswith("vmess://"):
        raw = url[len("vmess://"):].strip()
        raw += "=" * (-len(raw) % 4)
        try:
            data = json.loads(base64.b64decode(raw).decode("utf-8", "replace"))
        except Exception:
            die("не удалось расшифровать vmess-ссылку")
        host = str(data.get("add") or "")
        if not host:
            die("в vmess-ссылке нет адреса")
        port = int(data.get("port") or 443)
        tls = str(data.get("tls") or "").lower()
        ob = {"tag": "proxy", "protocol": "vmess",
              "settings": {"vnext": [{"address": host, "port": port, "users": [{
                  "id": str(data.get("id") or ""),
                  "alterId": int(data.get("aid") or 0),
                  "security": data.get("scy") or "auto"}]}]},
              "streamSettings": {"network": (data.get("net") or "tcp"),
                                 "security": "tls" if tls in ("tls", "true", "1", "reality") else "none"}}
        return ob, host, (lambda k, d=None: data.get(k) or d)

    die("поддерживаются ссылки vless://, vmess://, trojan://")


def main():
    url = sys.argv[1].strip()
    ob, host, g = build(url)
    ss = ob["streamSettings"]
    net, sec = ss["network"], ss["security"]
    sni = g("sni") or g("host") or host

    if sec == "tls":
        tls = {"serverName": sni}
        if g("fp"):
            tls["fingerprint"] = g("fp")
        if g("alpn"):
            tls["alpn"] = [a for a in str(g("alpn")).split(",") if a]
        ss["tlsSettings"] = tls
    elif sec == "reality":
        pbk = g("pbk")
        if not pbk:
            die("в reality-ключе нет pbk (публичного ключа)")
        real = {"serverName": sni, "fingerprint": g("fp") or "chrome",
                "publicKey": pbk, "shortId": g("sid") or ""}
        if g("spx"):
            real["spiderX"] = g("spx")
        ss["realitySettings"] = real

    if net == "ws":
        ws = {"path": up.unquote(g("path", "/") or "/")}
        if g("host"):
            ws["headers"] = {"Host": g("host")}
        ss["wsSettings"] = ws
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": g("serviceName") or ""}
    elif net in ("httpupgrade", "xhttp"):
        xset = {"path": up.unquote(g("path", "/") or "/"),
                "host": g("host") or ""}
        if net == "xhttp":
            xset["mode"] = g("mode") or "auto"
        ss[net + "Settings"] = xset
    elif net == "tcp" and g("headerType") == "http":
        ss["tcpSettings"] = {"header": {"type": "http"}}

    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "socks-in", "listen": "127.0.0.1", "port": 1080,
             "protocol": "socks", "settings": {"udp": True}},
            {"tag": "http-in", "listen": "127.0.0.1", "port": 8118,
             "protocol": "http"},
        ],
        "outbounds": [ob, {"tag": "direct", "protocol": "freedom"}],
        "routing": {"rules": [{"type": "field", "ip": ["geoip:private"],
                               "outboundTag": "direct"}]},
    }
    json.dump(cfg, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


main()
PYGEN
}

install_xray() {
    # Ставит Xray-core официальным установщиком, если его ещё нет.
    if command -v xray >/dev/null 2>&1; then
        say "  Xray уже установлен: $(xray version 2>/dev/null | head -1)"
        return 0
    fi
    say "  Xray-core не найден — устанавливаю..."
    if ! command -v curl >/dev/null 2>&1; then
        say "  !! Для установки Xray нужен curl."
        return 1
    fi
    local tmp; tmp="$(mktemp)"
    if ! curl -fsSL -m 90 -o "$tmp" \
        https://github.com/XTLS/Xray-install/raw/main/install-release.sh; then
        say "  !! Не удалось скачать установщик Xray (нет доступа к GitHub)."
        rm -f "$tmp"
        return 1
    fi
    if ! run sudo bash "$tmp" install >/tmp/xray-install.log 2>&1; then
        say "  !! Установка Xray не удалась:"
        tail -3 /tmp/xray-install.log 2>/dev/null | sed 's/^/     /'
        rm -f "$tmp"
        return 1
    fi
    rm -f "$tmp"
    command -v xray >/dev/null 2>&1
}

setup_telegram_proxy() {
    # setup_telegram_proxy ИМЯ_ПЕРЕМЕННОЙ — проверяет Telegram и настраивает
    # обход; в переменную попадает адрес прокси для бота (пусто = не нужен).
    local var="$1" url="" attempts=0 conf="" tmpdir=""
    printf -v "$var" '%s' ""

    if [ "$DRY_RUN" = "1" ]; then
        say "    [проверка] запрос api.telegram.org и настройка обхода"
        say "    [проверка] ключ -> $XRAY_CONF, systemctl enable --now xray"
        printf -v "$var" '%s' "http://127.0.0.1:8118"
        return 0
    fi

    if tg_reachable; then
        say "  Telegram доступен напрямую — обход не нужен."
        return 0
    fi

    say "  !! api.telegram.org не отвечает."
    say "     Похоже, сервер в России и Telegram заблокирован провайдером."
    local want=""
    ask_yn want "Настроить обход через VPN-ключ?" y
    if [ "$want" != "y" ]; then
        say "  Пропускаю. Без обхода бот не сможет работать с Telegram."
        say "  Позже можно вписать TG_PROXY вручную в .secrets/.env."
        return 0
    fi

    say ""
    say "  Подойдёт ссылка vless://, vmess:// или trojan://,"
    say "  а также готовый прокси вида socks5://host:port или http://host:port."
    say ""

    while [ "$attempts" -lt 3 ]; do
        attempts=$((attempts + 1))
        ask url "  Ключ или адрес прокси" ""
        if [ -z "$url" ]; then
            say "  Пусто — обход не настроен."
            return 0
        fi

        case "$url" in
            http://*|https://*|socks5://*|socks5h://*)
                say "  Проверяю прокси..."
                if proxy_works "$url"; then
                    say "  Прокси работает — Telegram проходит."
                    printf -v "$var" '%s' "$url"
                    return 0
                fi
                say "  !! Через этот прокси Telegram не открывается. Попробуйте другой."
                ;;

            vless://*|vmess://*|trojan://*)
                if ! command -v python3 >/dev/null 2>&1; then
                    say "  !! Для разбора ключа нужен python3."
                    return 1
                fi
                tmpdir="$(mktemp -d)"
                conf="$tmpdir/config.json"
                if ! gen_xray_config "$url" "$conf" 2>"$tmpdir/gen.err"; then
                    say "  !! Не удалось разобрать ключ: $(head -1 "$tmpdir/gen.err" 2>/dev/null)"
                    rm -rf "$tmpdir"
                    continue
                fi
                if ! grep -q '"outbounds"' "$conf"; then
                    say "  !! Ключ разобран неправильно, пропускаю."
                    rm -rf "$tmpdir"
                    continue
                fi
                install_xray || { rm -rf "$tmpdir"; continue; }

                say "  Проверяю конфиг Xray..."
                if ! sudo xray run -test -c "$conf" >"$tmpdir/test.log" 2>&1; then
                    say "  !! Ключ не прошёл проверку Xray:"
                    tail -3 "$tmpdir/test.log" | sed 's/^/     /'
                    rm -rf "$tmpdir"
                    continue
                fi

                run sudo mkdir -p "$(dirname "$XRAY_CONF")"
                run sudo cp "$conf" "$XRAY_CONF"
                run sudo chmod 644 "$XRAY_CONF"
                rm -rf "$tmpdir"

                run sudo systemctl enable xray >/dev/null 2>&1 || true
                run sudo systemctl restart xray
                sleep 2

                if proxy_works "socks5://127.0.0.1:1080" \
                   && proxy_works "http://127.0.0.1:8118"; then
                    say "  Готово: Telegram идёт через ваш ключ."
                    say "    SOCKS 127.0.0.1:1080, HTTP 127.0.0.1:8118"
                    printf -v "$var" '%s' "http://127.0.0.1:8118"
                    return 0
                fi
                say "  !! Ключ принят, но Telegram через него не открылся."
                say "     Пробую другой ключ."
                ;;

            *)
                say "  !! Не похоже на ключ или прокси."
                say "     Нужна ссылка vless://, vmess://, trojan:// или socks5://host:port."
                ;;
        esac
    done

    say "  !! Рабочий ключ не найден — обход не настроен."
    say "     Бот запустится, но Telegram будет молчать, пока не добавите TG_PROXY."
    return 0
}

# ---------------------------------------------------------------- установка
do_install() {
    local REPO_DIR="$SCRIPT_DIR" SECRETS_DIR="" git_url="$REPO_URL_DEFAULT"
    local TEAMTALK_HOST="" TEAMTALK_TCP_PORT="" TEAMTALK_UDP_PORT=""
    local TEAMTALK_USERNAME="" TEAMTALK_PASSWORD="" TEAMTALK_NICKNAME=""
    local TEAMTALK_CHANNEL="" TG_TOKEN="" TG_OWNER_USER_ID="" TG_NOTIFY_CHAT_ID=""
    local YT_COOKIES="" RT_COOKIES="" YM_TOKEN_VAL=""
    local CONFIRM="" START_NOW="" TG_PROXY_VAL=""

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
    ask TEAMTALK_NICKNAME "Имя бота (ник)" "UniversalBot"
    ask TEAMTALK_CHANNEL "Канал (пусто = корневой)" ""
    ask YM_TOKEN_VAL "OAuth-токен Яндекс.Музыки (пусто = пропустить)" ""
    ask TG_TOKEN "Токен Telegram-бота для реле (пусто = пропустить)" ""
    ask TG_OWNER_USER_ID "Ваш Telegram ID — владелец, пусто = пропустить" ""
    ask TG_NOTIFY_CHAT_ID "ID чата для уведомлений о входе/выходе, пусто = пропустить" ""
    ask YT_COOKIES "Путь к cookies.txt для YouTube (пусто = пропустить)" ""
    ask RT_COOKIES "Путь к rutube_cookies.txt (пусто = пропустить)" ""
    ask TG_MUSIC_TOKEN "Токен Telegram-бота для музыки, по желанию (пусто = пропустить)" ""

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
    say "== 3/7 Системные пакеты =="
    if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        say "  ffmpeg, python3, pip, venv..."
        run sudo apt-get update -y
        run sudo apt-get install -y ffmpeg python3 python3-pip python3-venv
    else
        say "  sudo/apt-get не найдены — пропускаю (пакеты нужно установить заранее)."
    fi

    # --- 4. python-окружение ---
    say ""
    say "== 4/7 Окружение Python (.venv) =="
    if [ -x "$REPO_DIR/.venv/bin/python" ]; then
        say "  .venv уже есть — обновляю зависимости."
    else
        run python3 -m venv "$REPO_DIR/.venv"
    fi
    run "$REPO_DIR/.venv/bin/python" -m pip install --upgrade pip
    run "$REPO_DIR/.venv/bin/python" -m pip install --upgrade yt-dlp yandex-music

    # --- 5. SDK ---
    say ""
    say "== 5/7 TeamTalk SDK =="
    local SDK_LIB=""
    SDK_LIB="$(ls -d "$REPO_DIR"/sdk/tt5sdk*/Library/TeamTalk_DLL/libTeamTalk5.so 2>/dev/null | head -1 || true)"
    if [ -n "$SDK_LIB" ]; then
        say "  SDK найден: $SDK_LIB"
    else
        say "  !! SDK не найден (sdk/tt5sdk*/Library/TeamTalk_DLL/libTeamTalk5.so)."
        say "     Проверьте, что репозиторий склонирован полностью. Без SDK бот не запустится."
    fi

    # --- 6. обход блокировки Telegram (только если нужен) ---
    say ""
    say "== 6/7 Обход блокировки Telegram =="
    if [ -n "$TG_TOKEN" ]; then
        setup_telegram_proxy TG_PROXY_VAL
    else
        say "  Токен Telegram-бота не задан — обход не требуется."
    fi

    # --- 7. секреты и запуск ---
    say ""
    say "== 7/7 Секреты и запуск =="
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
            if [ -n "$TG_PROXY_VAL" ]; then
                say "TG_PROXY=$(q "$TG_PROXY_VAL")"
            fi
            if [ -n "$TG_OWNER_USER_ID" ]; then
                say "TG_OWNER_USER_ID=$(q "$TG_OWNER_USER_ID")"
            fi
            if [ -n "$TG_NOTIFY_CHAT_ID" ]; then
                say "TG_NOTIFY_CHAT_ID=$(q "$TG_NOTIFY_CHAT_ID")"
            fi
            if [ -n "$TG_MUSIC_TOKEN" ]; then
                say "TG_MUSIC_TOKEN=$(q "$TG_MUSIC_TOKEN")"
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
    if [ -d "/run/systemd/system" ] && [ -f "$REPO_DIR/tt-plugin.service" ]; then
        mkdir -p "$UNIT_DIR"
        if [ "$DRY_RUN" = "1" ]; then
            say "    [проверка] sed __DIR__ -> $UNIT_DIR/$UNIT_NAME"
        else
            sed -e "s|__DIR__|$REPO_DIR|g" "$REPO_DIR/tt-plugin.service" > "$UNIT_DIR/$UNIT_NAME"
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
    if [ -n "$TG_PROXY_VAL" ]; then
        say "  Telegram: через $TG_PROXY_VAL"
    else
        say "  Telegram: напрямую"
    fi
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
