#!/bin/bash
# TeamTalk Universal Plugin — обёртка запуска.
#
#   ./run.sh                              конфиг рядом с bot.py (config.json)
#   ./run.sh --config /path/my-config.json   свой конфиг
#   ./run.sh --instance kirill-root          инстанс из общей папки инстансов
#   ./run.sh --show                          показать, с чем запустится, и выйти
#
# --instance берёт каталог из TT_INSTANCES_ROOT (по умолчанию /home/bot/ttplugin):
# сначала пробуется точное имя каталога, потом дефисы заменяются на слэши,
# то есть 'kirill-root' -> <root>/kirill/root/app, а 'druzhba-bez-granits'
# остаётся <root>/druzhba-bez-granits/app.
set -euo pipefail

die() { printf 'run.sh: %s\n' "$*" >&2; exit 2; }

DIR_DEFAULT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CALLER_PWD="$PWD"
DIR="$DIR_DEFAULT"
INSTANCE=""
CONFIG_ARG=""
SHOW=0
WAIT_SERVER=1

usage() {
    cat <<'EOF'
Запуск TeamTalk Universal Plugin.

  --config ПУТЬ, -c ПУТЬ   файл конфигурации (config.json)
  --instance ИМЯ, -i ИМЯ   инстанс из папки инстансов; дефис = вложенный путь
  --show                   напечатать итоговые пути и выйти
  --no-wait                не ждать, пока TeamTalk-сервер поднимет порт
  --help, -h               эта справка

Переменные окружения: CONFIG (путь к конфигу), TT_SDK_DIR, TT_INSTANCES_ROOT.
Приоритет конфига: --config > $CONFIG > config.json рядом с bot.py.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        -c|--config)
            [ $# -ge 2 ] || die "--config требует путь к файлу"
            CONFIG_ARG="$2"; shift 2 ;;
        --config=*)
            CONFIG_ARG="${1#*=}"; shift ;;
        -i|--instance)
            [ $# -ge 2 ] || die "--instance требует имя"
            INSTANCE="$2"; shift 2 ;;
        --instance=*)
            INSTANCE="${1#*=}"; shift ;;
        --show)
            SHOW=1; shift ;;
        --no-wait)
            WAIT_SERVER=0; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            die "неизвестный параметр: $1 (см. --help)" ;;
    esac
done

abspath() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        "~/"*) printf '%s\n' "$HOME/${1#\~/}" ;;
        *) printf '%s\n' "$CALLER_PWD/$1" ;;
    esac
}

# ---- инстанс: 'kirill-root' -> <root>/kirill/root/app ----
if [ -n "$INSTANCE" ]; then
    ROOT="${TT_INSTANCES_ROOT:-/home/bot/ttplugin}"
    [ -d "$ROOT" ] || die "папка инстансов не найдена: $ROOT"
    if [ -d "$ROOT/$INSTANCE/app" ]; then
        DIR="$ROOT/$INSTANCE/app"
    else
        DIR="$ROOT/${INSTANCE//-//}/app"
    fi
    [ -d "$DIR" ] || die "инстанс «$INSTANCE» не найден: нет каталога $DIR"
fi

[ -f "$DIR/bot.py" ] || die "в $DIR нет bot.py"

# ---- конфиг ----
if [ -n "$CONFIG_ARG" ]; then
    CONFIG_PATH="$(abspath "$CONFIG_ARG")"
    [ -f "$CONFIG_PATH" ] || die "файл конфигурации не найден: $CONFIG_PATH"
    export CONFIG="$CONFIG_PATH"
elif [ -n "${CONFIG:-}" ]; then
    CONFIG_PATH="$(abspath "$CONFIG")"
    [ -f "$CONFIG_PATH" ] || die "CONFIG указывает на несуществующий файл: $CONFIG_PATH"
    export CONFIG="$CONFIG_PATH"
else
    CONFIG_PATH="$DIR/config.json"
fi

# ---- TeamTalk SDK ----
SDK="${TT_SDK_DIR:-}"
if [ -z "$SDK" ]; then
    for cand in "$DIR/sdk/current" \
                "$DIR/sdk/tt5sdk_v5.23a_ubuntu26_x86_64" \
                "$DIR/sdk/tt5sdk_v5.22a_ubuntu22_x86_64"; do
        if [ -d "$cand/Library/TeamTalk_DLL" ]; then SDK="$cand"; break; fi
    done
fi
if [ -z "$SDK" ]; then
    SDK="$(ls -d "$DIR"/sdk/tt5sdk*/ 2>/dev/null | head -1 || true)"
    SDK="${SDK%/}"
fi
[ -n "$SDK" ] && [ -d "$SDK/Library/TeamTalk_DLL" ] || {
    printf 'run.sh: SDK не найден в %s/sdk — бот не сможет подключиться к TeamTalk\n' "$DIR" >&2
    SDK=""
}

LD_PARTS=""
[ -n "$SDK" ] && LD_PARTS="$SDK/Library/TeamTalk_DLL"
# $DIR/lib держит asound/pulse и прочие библиотеки, которых нет в системе
[ -d "$DIR/lib" ] && LD_PARTS="${LD_PARTS:+$LD_PARTS:}$DIR/lib"
export LD_LIBRARY_PATH="${LD_PARTS}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
[ -n "$SDK" ] && export PYTHONPATH="$SDK/Library/TeamTalkPy${PYTHONPATH:+:$PYTHONPATH}"

# ---- python ----
if [ -x "$DIR/.venv/bin/python" ]; then
    export PATH="$DIR/.venv/bin:$PATH"
    PY="$DIR/.venv/bin/python"
else
    PY="python3"
fi

# ---- секреты: сначала внутри каталога бота, потом уровнем выше ----
SECRETS_USED=""
for s in "$DIR/.secrets/.env" "$DIR/../.secrets/.env"; do
    if [ -f "$s" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$s"
        set +a
        SECRETS_USED="$s"
        break
    fi
done
export TEAMTALK_PASSWORD="${TEAMTALK_PASSWORD:-${TEAMTALK_BOT_PASSWORD:-}}"

# ---- порт сервера из конфига ----
PORT="$(python3 - "$CONFIG_PATH" <<'PYEOF' 2>/dev/null || true
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
    print(cfg.get("server", {}).get("tcp_port", "") or "")
except Exception:
    pass
PYEOF
)"
[ -n "$PORT" ] || PORT=10989

if [ "$SHOW" = 1 ]; then
    printf 'каталог бота: %s\n' "$DIR"
    printf 'конфиг:       %s\n' "$CONFIG_PATH"
    printf 'секреты:      %s\n' "${SECRETS_USED:-(не найдены)}"
    printf 'SDK:          %s\n' "${SDK:-(не найден)}"
    printf 'python:       %s\n' "$PY"
    printf 'порт сервера: %s\n' "$PORT"
    exit 0
fi

cd "$DIR"

if [ "$WAIT_SERVER" = 1 ]; then
    while ! /usr/bin/ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; do
        sleep 2
    done
fi

exec "$PY" bot.py
