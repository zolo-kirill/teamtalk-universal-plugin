#!/usr/bin/env bash
# Обход блокировки Telegram: проверка доступности и локальный прокси.
# Подключается после lib/common.sh.
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
