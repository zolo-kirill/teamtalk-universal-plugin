#!/usr/bin/env bash
# Компонент: зависимости.
# apt-пакеты, python-окружение (.venv), Deno, Silero TTS (torch), TeamTalk SDK.
#
#   bash components/deps.sh          поставить всё
#   bash components/deps.sh --check  только проверить, что уже стоит
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib/common.sh
. "$HERE/../lib/common.sh"
# shellcheck source=../lib/sdk.sh
. "$HERE/../lib/sdk.sh"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

require_non_root
ensure_repo || exit 1

say ""
say "== Зависимости =="

# ---- 1. системные пакеты ----
if [ "$CHECK_ONLY" = 1 ]; then
    say "  проверка системных пакетов:"
else
    if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        say "  ffmpeg, python3 (pip, venv), curl, ca-certificates, unzip, git, p7zip..."
        run sudo apt-get update -y
        run sudo apt-get install -y ffmpeg python3 python3-pip python3-venv \
            curl ca-certificates wget unzip git p7zip-full
    else
        say "  sudo/apt-get не найдены — пропускаю (пакеты нужно установить заранее)."
    fi
fi
for bin in ffmpeg python3 curl; do
    if command -v "$bin" >/dev/null 2>&1; then
        [ "$CHECK_ONLY" = 1 ] && say "    $bin: $(command -v "$bin")"
    else
        say "  !! не найден $bin — без него бот не заработает."
    fi
done

# ---- 2. python-окружение ----
say ""
say "== Окружение Python (.venv) =="
if [ -x "$REPO_DIR/.venv/bin/python" ]; then
    say "  .venv уже есть: $REPO_DIR/.venv"
    [ "$CHECK_ONLY" = 1 ] || say "  обновляю зависимости."
else
    if [ "$CHECK_ONLY" = 1 ]; then
        say "  !! .venv нет — запустите без --check."
        exit 1
    fi
    run python3 -m venv "$REPO_DIR/.venv"
fi
PY="$REPO_DIR/.venv/bin/python"
if [ "$CHECK_ONLY" = 0 ]; then
    run "$PY" -m pip install --upgrade pip wheel
    run "$PY" -m pip install --upgrade requests yt-dlp yandex-music
fi

# ---- 3. Deno для yt-dlp ----
if command -v deno >/dev/null 2>&1; then
    say "  Deno: $(command -v deno)"
elif [ "$CHECK_ONLY" = 0 ]; then
    local_deno="n"
    ask_yn local_deno "  Поставить Deno (нужен yt-dlp для YouTube)" y
    if [ "$local_deno" = "y" ]; then
        say "  Ставлю Deno в /usr/local/bin..."
        if run sudo env DENO_INSTALL=/usr/local sh -c 'curl -fsSL https://deno.land/install.sh | sh' >/dev/null 2>&1; then
            say "  Deno: $(command -v deno || echo 'не найден')"
        else
            say "  !! Deno не установился — YouTube может отдавать не все форматы."
        fi
    fi
else
    say "  Deno не найден (для YouTube необязателен, но полезен)."
fi

# ---- 4. Silero TTS (torch) ----
if "$PY" -c "import torch" >/dev/null 2>&1; then
    say "  Silero TTS: torch уже стоит"
elif [ "$CHECK_ONLY" = 0 ]; then
    local_voice="n"
    ask_yn local_voice "  Поставить голосовое приветствие (Silero TTS, ~200 МБ)" y
    if [ "$local_voice" = "y" ]; then
        say "  Ставлю torch (CPU) и numpy..."
        if run "$PY" -m pip install --quiet --index-url https://download.pytorch.org/whl/cpu torch numpy; then
            say "  Silero TTS: готово, голос задаётся в welcome.voice_speaker"
        else
            say "  !! torch не установился — голосовое приветствие будет молчать."
        fi
    fi
else
    say "  Silero TTS: torch не стоит (голосовые приветствия будут молчать)."
fi

# ---- 5. TeamTalk SDK ----
say ""
say "== TeamTalk SDK =="
SDK_LIB=""
SDK_DIR=""
BUNDLED="$(ls -d "$REPO_DIR"/sdk/tt5sdk*/Library/TeamTalk_DLL/libTeamTalk5.so 2>/dev/null | head -1 || true)"
if [ "$CHECK_ONLY" = 0 ]; then
    SDK_DIR="$(sdk_install_best "$REPO_DIR" "$PY" || true)"
else
    SDK_DIR="$(cd "$REPO_DIR/sdk/current" 2>/dev/null && pwd || true)"
fi
if [ -n "$SDK_DIR" ] && [ -f "$SDK_DIR/Library/TeamTalk_DLL/libTeamTalk5.so" ]; then
    SDK_LIB="$SDK_DIR/Library/TeamTalk_DLL/libTeamTalk5.so"
elif [ -n "$BUNDLED" ]; then
    SDK_LIB="$BUNDLED"
    SDK_DIR="$(cd "$(dirname "$BUNDLED")/../.." && pwd)"
    say "  Беру SDK из репозитория: $(basename "$SDK_DIR")"
fi
if [ -n "$SDK_LIB" ]; then
    [ "$CHECK_ONLY" = 0 ] && run ln -sfn "$(basename "$SDK_DIR")" "$REPO_DIR/sdk/current"
    say "  SDK: $SDK_DIR"
    if sdk_test_load "$SDK_DIR" "$PY"; then
        say "  Импорт TeamTalk5: OK"
    else
        say "  !! TeamTalk5 не импортируется — проверьте SDK вручную."
        [ "$CHECK_ONLY" = 1 ] && exit 1
    fi
else
    say "  !! SDK не найден и не скачался. Без него бот не запустится."
    say "     Скачайте вручную: https://bearware.dk/?page_id=419"
    say "     и распакуйте в $REPO_DIR/sdk/"
    [ "$CHECK_ONLY" = 1 ] && exit 1
fi

say ""
say "== Зависимости готовы =="
say "  $CREATOR_LINE"
