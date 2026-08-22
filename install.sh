#!/usr/bin/env bash
# Interactive installer for the TeamTalk Music Bot.
# Menu: install / uninstall / exit. Asks all config, clones the repo,
# installs (apt, venv, SDK check, .secrets, systemd service) and starts the bot.
#
# Created by zolo-kirill. For any questions contact @zolo-kirill on Telegram.
#
# Usage: bash install.sh
# Test mode (no real actions): SETUP_DRY_RUN=1 bash install.sh
set -euo pipefail

CREATOR_LINE="Created by zolo-kirill. For any questions contact @zolo-kirill on Telegram."
REPO_URL_DEFAULT="https://github.com/zolo-kirill/teamtalk-music-bot.git"
UNIT_NAME="teamtalk-music-bot.service"
UNIT_DIR="$HOME/.config/systemd/user"
DRY_RUN="${SETUP_DRY_RUN:-0}"

say() { printf '%s\n' "$*"; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        say "    [dry] $*"
        return 0
    fi
    "$@"
}

# ask VARNAME "Prompt" [default] — read a line into a variable (handles spaces).
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
    # ask_yn VARNAME "Prompt" [default y|n]
    local var="$1" prompt="$2" default="${3:-n}" ans
    while :; do
        read -r -p "$prompt (y/n) [$default]: " ans
        [ -z "$ans" ] && ans="$default"
        case "$ans" in
            y|Y|yes) printf -v "$var" 'y'; return 0 ;;
            n|N|no)  printf -v "$var" 'n'; return 0 ;;
            *) say "   Answer y or n." ;;
        esac
    done
}

# q VALUE — shell-safe single quotes: '$', '`', '\', spaces — all literal.
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
    say "!! Running as root. The installer sets up a user service."
    say "   Exit root and run it as a normal user:"
    say "   sudo bash install.sh"
    exit 1
fi

# ---------------------------------------------------------------- install
do_install() {
    local REPO_DIR="$SCRIPT_DIR" SECRETS_DIR="" git_url="$REPO_URL_DEFAULT"
    local TEAMTALK_HOST="" TEAMTALK_TCP_PORT="" TEAMTALK_UDP_PORT=""
    local TEAMTALK_USERNAME="" TEAMTALK_PASSWORD="" TEAMTALK_NICKNAME=""
    local TEAMTALK_CHANNEL="" TG_TOKEN="" YT_COOKIES="" RT_COOKIES="" YM_TOKEN_VAL=""
    local CONFIRM="" START_NOW=""

    say ""
    say "== Install TeamTalk Music Bot =="

    # --- 1. repository (clone if no bot next to the installer) ---
    if [ ! -f "$REPO_DIR/bot.py" ]; then
        say "  bot.py not found next to the installer — cloning the repository."
        ask git_url "Repository URL" "$REPO_URL_DEFAULT"
        ask REPO_DIR "Clone to directory?" "$HOME/teamtalk-music-bot"
        REPO_DIR="${REPO_DIR/#\~/$HOME}"
        if [ -e "$REPO_DIR" ]; then
            if [ -f "$REPO_DIR/bot.py" ]; then
                say "  $REPO_DIR already contains the bot — using as-is."
            else
                ask_yn REPLACE "Directory $REPO_DIR exists but has no bot. Clear and clone again?" n
                if [ "$REPLACE" != "y" ]; then
                    say "  Install aborted."
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

    # --- 2. questions (server, login and password are required) ---
    say ""
    say "  Fill in the TeamTalk parameters:"
    while :; do
        ask TEAMTALK_HOST "TeamTalk server (host)" ""
        ask TEAMTALK_TCP_PORT "TCP port" "10333"
        ask TEAMTALK_UDP_PORT "UDP port" "10333"
        ask TEAMTALK_USERNAME "Login" ""
        ask_secret TEAMTALK_PASSWORD "Password"
        if [ -n "$TEAMTALK_HOST" ] && [ -n "$TEAMTALK_USERNAME" ] && [ -n "$TEAMTALK_PASSWORD" ]; then
            break
        fi
        say "  !! Server, login and password are required. Try again."
    done
    ask TEAMTALK_NICKNAME "Bot nickname" "MusicBot"
    ask TEAMTALK_CHANNEL "Channel (empty = root channel)" ""
    ask YM_TOKEN_VAL "Yandex Music OAuth token (empty = skip)" ""
    ask TG_TOKEN "Telegram bot token for the relay (empty = skip)" ""
    ask YT_COOKIES "Path to cookies.txt for YouTube (empty = skip)" ""
    ask RT_COOKIES "Path to rutube_cookies.txt (empty = skip)" ""

    say ""
    say "  Review:"
    say "    Server:    $TEAMTALK_HOST:$TEAMTALK_TCP_PORT (udp $TEAMTALK_UDP_PORT)"
    say "    Login:     $TEAMTALK_USERNAME  (nick: $TEAMTALK_NICKNAME)"
    if [ -n "$TEAMTALK_CHANNEL" ]; then
        say "    Channel:   $TEAMTALK_CHANNEL"
    else
        say "    Channel:   (root channel)"
    fi
    say "    Password:  ***"
    ask_yn CONFIRM "Everything correct? Proceed with the install" y
    if [ "$CONFIRM" != "y" ]; then
        say "  Install aborted."
        return 1
    fi

    # --- 3. system packages ---
    say ""
    say "== 3/6 System packages =="
    if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        say "  ffmpeg, python3, pip, venv..."
        run sudo apt-get update -y
        run sudo apt-get install -y ffmpeg python3 python3-pip python3-venv
    else
        say "  sudo/apt-get not found — skipping (packages must be installed beforehand)."
    fi

    # --- 4. python environment ---
    say ""
    say "== 4/6 Python environment (.venv) =="
    if [ -x "$REPO_DIR/.venv/bin/python" ]; then
        say "  .venv already exists — updating dependencies."
    else
        run python3 -m venv "$REPO_DIR/.venv"
    fi
    run "$REPO_DIR/.venv/bin/python" -m pip install --upgrade pip
    run "$REPO_DIR/.venv/bin/python" -m pip install --upgrade yt-dlp yandex-music

    # --- 5. SDK ---
    say ""
    say "== 5/6 TeamTalk SDK =="
    local SDK_LIB=""
    SDK_LIB="$(ls -d "$REPO_DIR"/sdk/tt5sdk*/*/Library/TeamTalk_DLL/libTeamTalk5.so 2>/dev/null | head -1 || true)"
    if [ -n "$SDK_LIB" ]; then
        say "  SDK found: $SDK_LIB"
    else
        say "  !! SDK not found (sdk/tt5sdk*/Library/TeamTalk_DLL/libTeamTalk5.so)."
        say "     Check that the repository was cloned completely. The bot won't start without the SDK."
    fi

    # --- 6. secrets and launch ---
    say ""
    say "== 6/6 Secrets and launch =="
    mkdir -p "$SECRETS_DIR"
    local ENV_FILE="$SECRETS_DIR/.env"
    if [ -f "$ENV_FILE" ]; then
        local BAK="$ENV_FILE.bak.$(date +%s)"
        run cp "$ENV_FILE" "$BAK"
        say "  Old .env saved as $BAK"
    fi
    if [ "$DRY_RUN" = "1" ]; then
        say "    [dry] writing $ENV_FILE"
    else
        {
            say "# TeamTalk Music Bot — configuration (generated $(date -Iseconds))"
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
        } > "$ENV_FILE"
        say "  Config written: $ENV_FILE"
    fi

    if [ -n "$YT_COOKIES" ]; then
        if [ -f "$YT_COOKIES" ]; then
            run cp "$YT_COOKIES" "$SECRETS_DIR/cookies.txt"
            say "  cookies.txt (YouTube) copied to .secrets/"
        else
            say "  !! File not found: $YT_COOKIES — skipping YouTube cookies."
        fi
    fi
    if [ -n "$RT_COOKIES" ]; then
        if [ -f "$RT_COOKIES" ]; then
            run cp "$RT_COOKIES" "$SECRETS_DIR/rutube_cookies.txt"
            say "  rutube_cookies.txt copied to .secrets/"
        else
            say "  !! File not found: $RT_COOKIES — skipping Rutube cookies."
        fi
    fi
    if [ -n "$YM_TOKEN_VAL" ]; then
        run sh -c 'printf "%s" "$1" > "$2"' _ "$YM_TOKEN_VAL" "$SECRETS_DIR/ym_token.txt"
        say "  Yandex Music OAuth token written to .secrets/ym_token.txt"
    fi

    # systemd service (if present) or a background start
    if [ -d "/run/systemd/system" ] && [ -f "$REPO_DIR/teamtalk-music-bot.service" ]; then
        mkdir -p "$UNIT_DIR"
        if [ "$DRY_RUN" = "1" ]; then
            say "    [dry] sed __DIR__ -> $UNIT_DIR/$UNIT_NAME"
        else
            sed -e "s|__DIR__|$REPO_DIR|g" "$REPO_DIR/teamtalk-music-bot.service" > "$UNIT_DIR/$UNIT_NAME"
        fi
        run systemctl --user daemon-reload
        run systemctl --user enable "$UNIT_NAME"
        say "  Starting the bot..."
        run systemctl --user start "$UNIT_NAME"
        if command -v loginctl >/dev/null 2>&1; then
            run sudo loginctl enable-linger "$(id -un)" || say "  (couldn't enable linger — after a reboot the service starts only after login)"
        fi
    else
        say "  systemd not found — autostart skipped."
        ask_yn START_NOW "Start the bot now (in background)?" y
        if [ "$START_NOW" = "y" ]; then
            run bash -c "cd \"$REPO_DIR\" && nohup bash run.sh > bot.log 2>&1 &"
            say "  Bot started in background. Log: $REPO_DIR/bot.log"
        else
            say "  Manual start: bash $REPO_DIR/run.sh"
        fi
    fi

    say ""
    say "== Install complete =="
    say "  Directory: $REPO_DIR"
    say "  Secrets:   $SECRETS_DIR/.env"
    if [ -d "/run/systemd/system" ]; then
        say "  Status:    systemctl --user status $UNIT_NAME"
        say "  Logs:      journalctl --user -u $UNIT_NAME -f"
    fi
    say "  $CREATOR_LINE"
}

# --------------------------------------------------------------- uninstall
do_uninstall() {
    local repo="" secrets="" unit="$UNIT_DIR/$UNIT_NAME"

    say ""
    say "== Uninstall TeamTalk Music Bot =="

    # find the repository: next to the installer or from the service ExecStart
    if [ -f "$SCRIPT_DIR/bot.py" ]; then
        repo="$SCRIPT_DIR"
    elif [ -f "$unit" ]; then
        repo="$(sed -n 's|^ExecStart=/bin/bash \(.*\)/run\.sh$|\1|p' "$unit" | head -1 || true)"
    fi
    if [ -n "$repo" ]; then
        repo="$(cd "$repo" 2>/dev/null && pwd || echo "")"
    fi

    if [ -z "$repo" ] && [ ! -f "$unit" ]; then
        say "  Couldn't find an installed bot: neither the service $unit nor bot.py nearby."
        say "  Nothing to remove."
        return 0
    fi

    # stop and remove the service
    if [ -f "$unit" ]; then
        if [ -d "/run/systemd/system" ]; then
            say "  Stopping the service..."
            run systemctl --user stop "$UNIT_NAME" || true
            run systemctl --user disable "$UNIT_NAME" || true
        fi
        run rm -f "$unit"
        if [ -d "/run/systemd/system" ]; then
            run systemctl --user daemon-reload
        fi
        say "  Service stopped and removed."
    else
        say "  systemd service not installed (skipping)."
    fi

    # remove the repository (with .venv)
    if [ -n "$repo" ]; then
        say ""
        say "  Repository: $repo"
        ask_yn DEL_REPO "Delete it (including .venv)?" n
        if [ "$DEL_REPO" = "y" ]; then
            run rm -rf "$repo"
            say "  Repository deleted."
            secrets="$(dirname "$repo")/.secrets"
        else
            say "  Repository kept."
        fi
    fi

    # remove secrets
    if [ -n "$repo" ] && [ -d "$(dirname "$repo")/.secrets" ]; then
        secrets="$(dirname "$repo")/.secrets"
    fi
    if [ -n "$secrets" ] && [ -d "$secrets" ]; then
        say ""
        say "  Secrets: $secrets"
        ask_yn DEL_SECRETS "Delete the secrets folder (password, tokens)?" n
        if [ "$DEL_SECRETS" = "y" ]; then
            run rm -rf "$secrets"
            say "  Secrets deleted."
        fi
    fi

    say ""
    say "== Bot removed. Bye! =="
    say "  $CREATOR_LINE"
}

# --------------------------------------------------------------------- menu
menu() {
    while :; do
        say ""
        say "What do you want to do?"
        say "  1) Install the bot"
        say "  2) Uninstall the bot"
        say "  3) Exit"
        local choice
        read -r -p "Choose (1-3): " choice
        case "$choice" in
            1) do_install ;;
            2) do_uninstall ;;
            3) say "Bye!"; exit 0 ;;
            *) say "  No such option — choose 1, 2 or 3." ;;
        esac
    done
}

say "== TeamTalk Music Bot — installer =="
say "  $CREATOR_LINE"
if [ "$DRY_RUN" = "1" ]; then
    say "  (dry-run mode SETUP_DRY_RUN=1 — no real actions performed)"
fi
menu
