#!/usr/bin/env python3
"""Универсальный плагин для TeamTalk-сервера: музыка в голосовой канал,
реле сообщений и файлов в Telegram, защита от ботнетов, регистрация учёток."""
import array
import ctypes
import json
import os
import queue
import re
import secrets
import select
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
import bisect
import glob
import ipaddress
from collections import deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ctypes import byref

import TeamTalk5
from TeamTalk5 import (
    TextMsgType,
    buildTextMessage,
    MediaFileStatus,
    MediaFilePlayback,
    StreamType,
    TT_MEDIAPLAYBACK_OFFSET_IGNORE,
)


def _b(s):
    """TeamTalk ctypes bindings expect UTF-8 bytes (c_char_p) on Linux."""
    return s.encode("utf-8") if isinstance(s, str) else s

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


# ---- признаки ботнет-клиентов (массовые входы «мёртвых» ботов) ----
# Флуд-ники вида «🤖 Shadow_Pilot_46»: эмодзи + латинское слово_слово_число.
# Кириллические ники и одиночные подчёркивания («kirill_mobile») не трогаем.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U00002190-\U000021FF\U0000FE0F\U00002300-\U000023FF]"
)
_BOTNET_WORD_RE = re.compile(r"[A-Za-z]{2,}_[A-Za-z0-9]{1,}(?:_[0-9]{1,})?")


def _is_botnet_nick(nick):
    """Эвристика ботнет-ника: не пустой, >=2 подчёркиваний и латинское
    слово_слово. Без эмодзи требуем >=3 подчёркиваний, чтобы не ловить
    обычные ники вида «my_nick»."""
    n = (nick or "").strip()
    if not n or n.count("_") < 2:
        return False
    has_emoji = bool(_EMOJI_RE.search(n))
    has_word = bool(_BOTNET_WORD_RE.search(n))
    if not has_word:
        return False
    if has_emoji:
        return True
    return n.count("_") >= 3


# ---- признаки не-дружелюбного ника (масса текста или мат) ----
# Гости могут вписать в «ник» целую простыню/оскорбление и светить её в списке
# пользователей. Слишком длинный или нецензурный ник режем киком + баном IP,
# не дожидаясь гео (российский IP фильтр стран не ловит). Нормальный ник всегда
# короче 40 символов — порог настраивается в guard.max_nick_len (0 = выкл).
_DEFAULT_NICK_MAX_LEN = 40

# Корни мата (после нормализации ё->е и lower). Подбор консервативный: только
# однозначные обсценные корни, чтобы «хулиган», «лебедь», «мудрый», «херсон»
# (слова с похожими подстроками) НЕ ловились. Настраивается в
# guard.mat_words (свой список вместо встроенного).
_DEFAULT_MAT_ROOTS = (
    "пизд", "хуй", "хуе", "хуя", "бля", "еба", "ебл", "ебн", "ебок",
    "заеб", "наеб", "поеб", "говн", "гандон", "залуп", "шлюх", "дроч",
    "манд", "мудак", "мудач", "пидор", "пидр", "сука", "суки", "суку",
    "херн", "херо",
)


def _compile_mat_re(words):
    """Регэксп по списку корней (ё->е, lower). Пусто/None при пустом списке."""
    pats = []
    for w in words or []:
        w = str(w).strip().lower().replace("ё", "е")
        if len(w) >= 2:
            pats.append(re.escape(w))
    if not pats:
        return None
    return re.compile("|".join(pats))


# ---- конфиг: config_default.json (эталон) + config.json (правки сервера) ----
# config.json в .gitignore и перекрывает config_default.json; env-переменные —
# фолбэк на случай, когда ключа нет ни в одном из файлов.
def _deep_merge(base, over):
    out = dict(base or {})
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


CONFIG_PATH = os.environ.get("CONFIG") or os.path.join(BASE_DIR, "config.json")
CFG = _deep_merge(
    _load_json(os.path.join(BASE_DIR, "config_default.json")),
    _load_json(CONFIG_PATH),
)


def _cfg(path, env=None, default=None):
    """Value from CFG by dotted path; env var and default as fallback."""
    node = CFG
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            node = None
            break
        node = node[part]
    if isinstance(node, (dict, list)):
        if node:
            return node
    elif node is not None and node != "":
        return node
    if env and env in os.environ and os.environ.get(env) != "":
        return os.environ[env]
    return default


def _save_config(patch):
    """Deep-merge `patch` into the on-disk config (CONFIG_PATH) and rewrite the
    file atomically, so runtime changes survive a restart."""
    data = _load_json(CONFIG_PATH)
    stack = [(data, patch)]
    while stack:
        dst, src = stack.pop()
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                stack.append((dst[k], v))
            else:
                dst[k] = v
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


HOST = str(_cfg("server.host", "TEAMTALK_HOST", "example.com"))
TCP_PORT = int(_cfg("server.tcp_port", "TEAMTALK_TCP_PORT", 10333))
UDP_PORT = int(_cfg("server.udp_port", "TEAMTALK_UDP_PORT", 10333))
NICKNAME = str(_cfg("server.nickname", "TEAMTALK_NICKNAME", "UniversalBot"))
USERNAME = str(_cfg("server.username", "TEAMTALK_USERNAME", "example"))
PASSWORD = str(_cfg("server.password", "TEAMTALK_PASSWORD", ""))
CHANNEL = str(_cfg("server.channel", "TEAMTALK_CHANNEL", ""))  # empty = root channel
CHANNEL_PASSWORD = str(_cfg("server.channel_password", "TEAMTALK_CHANNEL_PASSWORD", ""))
DEFAULT_VOLUME = int(_cfg("playback.default_volume", None, 10))
MAX_VOLUME = int(_cfg("playback.max_volume", None, 100))
DEFAULT_SERVICE = str(_cfg("runtime.main_service", None, "yt"))
DEFAULT_CHANNEL_MSG = bool(_cfg("runtime.send_to_channel", None, True))
START_COMMANDS = list(_cfg("runtime.startup_commands", None, []))
CLIENTNAME = "teamtalk-universal-plugin"
CONNECT_TIMEOUT = int(_cfg("server.connect_timeout_sec", None, 20))

CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
INBOX_DIR = os.path.join(BASE_DIR, "inbox")  # files relayed from Telegram
os.makedirs(INBOX_DIR, exist_ok=True)

STATUS_MSG_FILE = os.path.join(BASE_DIR, ".statusmsg")
CHANNEL_MSG_FILE = os.path.join(BASE_DIR, ".channel_msg")
VOICE_ANNOUNCE_FILE = os.path.join(BASE_DIR, ".voice_announce")
FAVORITES_FILE = os.path.join(BASE_DIR, "favorites.json")
SUBS_FILE = os.path.join(BASE_DIR, "subs.json")
SUB_TTL_SEC = 86400  # сколько живёт ссылка-подписка (24 ч)
ADMINS_FILE = os.path.join(BASE_DIR, "users.db")  # user id администраторов Telegram
BANS_FILE = os.path.join(BASE_DIR, "bans.json")  # кого бот забанил через Telegram (для /unban)
REPLIES_FILE = os.path.join(BASE_DIR, "replies.json")  # пересланные сообщения TeamTalk: tg_message_id → tt_user_id
REPLY_TTL_SEC = 3600  # сколько можно ответить на пересланное сообщение (1 ч, как в sender-rs)
MUSIC_SUBS_FILE = os.path.join(BASE_DIR, "music_subs.json")  # подписчики на музыку в Telegram
MUSIC_SUB_TTL_SEC = 86400  # сколько живёт ссылка подписки на музыку (24 ч)

TG_TOKEN = str(_cfg("telegram.token", "TG_TOKEN", "")).strip()  # optional: own Telegram bot that relays files
TG_OWNER_USER_ID = int(_cfg("telegram.owner_user_id", "TG_OWNER_USER_ID", 0) or 0)  # only this user can send commands
TG_NOTIFY_CHAT_ID = int(_cfg("telegram.notify_chat_id", "TG_NOTIFY_CHAT_ID", 0) or 0)  # сюда слать вход/выход пользователей (0 = выкл)
TG_NOTIFY_SERVER = str(_cfg("telegram.server_display_name", "TG_NOTIFY_SERVER_NAME", "")).strip()  # пусто = брать имя сервера из TeamTalk
TG_NOTIFY_IGNORE = {u.strip().lower() for u in (_cfg("telegram.ignore_usernames", None, []) or []) if u.strip()}
TG_NOTIFY_IGNORE.add("bot_admin")  # все боты на одной админ-учётке — их не анонсируем

# Приветствие при входе пользователя на сервер: просьба ознакомиться с правилами
# (welcome.rules_text в config.json; пусто — стандартная строка).
WELCOME_RULES = str(_cfg("welcome.rules_text", None, "") or "").strip()

# Отдельный Telegram-бот для музыки: подписчики (sub mus) получают играющие треки.
# Пусто — музыкальный бот не подключён (sub mus отвечает, что не настроен).
TG_MUSIC_TOKEN = str(_cfg("telegram.music_token", "TG_MUSIC_TOKEN", "")).strip()

# Регистратор учётных записей TeamTalk (модуль tt_register.py): отдельный
# Telegram-бот принимает заявки (логин + пароль), админ принимает/отклоняет,
# при принятии бот создаёт учётку и шлёт сетевое сообщение. Пустой токен —
# модуль не запускается.
REG_ENABLED = bool(_cfg("registration.enabled", None, False))
REG_TOKEN = str(_cfg("registration.token", "TG_REG_TOKEN", "")).strip()
REG_ADMIN_USER_IDS = [int(x) for x in (_cfg("registration.notify_user_ids", None, []) or []) if x]
REG_BROADCAST_TEXT = str(_cfg("registration.broadcast_text", None, "")).strip()
REG_ADMIN_TT_USER = str(_cfg("registration.admin_username", "TEAMTALK_ADMIN_USER", "bot_admin")).strip()
REG_ADMIN_TT_PASS = str(_cfg("registration.admin_password", "TEAMTALK_ADMIN_PASSWORD", "")).strip()
REG_ADMIN_TT_NICK = str(_cfg("registration.admin_nickname", None, "регистратор")).strip()

# Optional YouTube cookies to bypass bot-check on restricted videos.
COOKIES = _cfg("services.yt.cookiefile_path", "TEAMTALK_COOKIES", None) or os.path.join(
    BASE_DIR, "..", ".secrets", "cookies.txt"
)
if not os.path.isfile(COOKIES):
    COOKIES = None

# Optional Rutube cookies for auth-gated video downloads (search stays blocked by their bot-protection).
RUTUBE_COOKIES = _cfg("services.rt.cookiefile_path", "TEAMTALK_RUTUBE_COOKIES", None) or os.path.join(
    BASE_DIR, "..", ".secrets", "rutube_cookies.txt"
)
if not os.path.isfile(RUTUBE_COOKIES):
    RUTUBE_COOKIES = None


def _fresh_cookies(src=None):
    """yt-dlp rewrites the --cookies file in place on every run, stripping the
    signed-in auth cookies (SID/PSID/etc). Hand it a per-run writable COPY so
    the master file keeps the full session and YouTube bot-check stays solved."""
    src = src or COOKIES
    if not src or not os.path.isfile(src):
        return None
    dst = os.path.join(CACHE_DIR, "ck_%s.txt" % uuid.uuid4().hex[:10])
    try:
        shutil.copyfile(src, dst)
        return dst
    except Exception:
        return None


def _drop_cookies(path):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except Exception:
            pass


def _find_downloaded(out):
    """Файл, который yt-dlp реально создал для префикса out.*. Скачивание идёт
    без перекодирования в mp3, так что расширение заранее неизвестно
    (webm/m4a/mp3/…) — ищем по префиксу. Возвращает путь или None."""
    try:
        for p in sorted(glob.glob(out + ".*")):
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return p
    except Exception:
        pass
    return None


def _looks_like_cookie_export(text):
    """Владелец прислал экспорт кук (Netscape): строка с шапкой + youtube.com."""
    t = (text or "").lstrip()
    return t.startswith("# Netscape HTTP Cookie File") or (
        t.startswith(".youtube.com") and "youtube.com" in t
    )


def _normalize_cookie_text(text):
    """Экспорт кук → tab-разделяемый Netscape-файл только с youtube-куками."""
    out = []
    for ln in (text or "").splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        f = ln.split()
        dom = f[0].lstrip(".")
        if dom != "youtube.com" and not dom.endswith(".youtube.com"):
            continue
        if len(f) == 6:
            f.append("")  # пустое значение
        if len(f) != 7:
            raise ValueError("строка не из 7 полей: %s" % ln[:80])
        out.append("\t".join(f))
    if not out:
        raise ValueError("нет youtube-кук в экспорте")
    return "# Netscape HTTP Cookie File\n" + "\n".join(out) + "\n"


# Optional Yandex Music OAuth token (config services.ym.token, else .secrets/ym_token.txt).
YM_TOKEN = str(_cfg("services.ym.token", None, "")).strip()
if not YM_TOKEN:
    _ym_path = os.environ.get("TEAMTALK_YM_TOKEN") or os.path.join(
        BASE_DIR, "..", ".secrets", "ym_token.txt"
    )
    if os.path.isfile(_ym_path):
        YM_TOKEN = open(_ym_path, encoding="utf-8").read().strip()

URL_RE = re.compile(r"https?://\S+", re.I)

# FFMpeg/yt-dlp resolve via PATH
YTDLP = sys.executable and [sys.executable, "-m", "yt_dlp"]

# JS runtime for yt-dlp challenge solving (deno binary; override via config/env).
YT_JS_RUNTIME = os.environ.get("YT_JS_RUNTIME")
if YT_JS_RUNTIME is None:
    YT_JS_RUNTIME = _cfg("runtime.yt_js_runtime", None, None)
if not YT_JS_RUNTIME:
    YT_JS_RUNTIME = shutil.which("deno") or "/home/superlisa/.local/bin/deno"

# YouTube po_token provider extractor-arg; empty disables it (e.g. no bgutil server).
YT_PO_TOKEN = os.environ.get("YT_PO_TOKEN_EXTRACTOR")
if YT_PO_TOKEN is None:
    YT_PO_TOKEN = _cfg("runtime.yt_po_token_extractor", None, None)
    if YT_PO_TOKEN is None:
        YT_PO_TOKEN = "youtube:po_token_provider=bgutil:http"

# Max tracks loaded from a playlist (YouTube / Yandex Music). High default so big
# playlists («Мне нравится» ≈ тысячи треков) load fully; override via config.
PLAYLIST_LIMIT = int(_cfg("runtime.playlist_limit", None, 5000))

# Voice transmission: raw PCM fed to TT_InsertAudioBlock as STREAMTYPE_VOICE.
# Стерео: серверные каналы стоят на Opus 48000/2ch, так что шлём 2 канала.
VOICE_RATE = 48000  # Hz
VOICE_CHANNELS = 2
VOICE_CHUNK = 960   # samples per channel per block (20 ms at 48 kHz)
VOICE_CHUNK_BYTES = VOICE_CHUNK * 2 * VOICE_CHANNELS  # s16 interleaved

# Playback through PulseAudio: when set, the track is played by ffmpeg into
# this sink and captured back from its monitor, so anything audible on the
# machine can be routed into the channel. Empty = decode straight to PCM.
PULSE_SINK = _cfg("playback.pulse_sink", None, "") or ""


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _fmt_ms(ms):
    s = int(ms) // 1000
    return "%d:%02d" % (s // 60, s % 60)


def _restart_bot_soon():
    """Exit the process shortly; the service supervisor (restart=always) relaunches."""
    time.sleep(1.5)
    log("exiting for restart")
    os._exit(0)


def _nightly_restart_delay(tzname="Europe/Moscow", hour=3, minute=0):
    """Секунды до ближайшего ночного перезапуска (по московскому времени)."""
    now = datetime.now(ZoneInfo(tzname))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _start_nightly_restart():
    """Daemon-поток: перезапуск бота каждую ночь в 03:00 МСК."""
    def loop():
        while True:
            delay = _nightly_restart_delay()
            log("nightly restart in %.0fs" % delay)
            time.sleep(delay)
            log("nightly restart")
            _restart_bot_soon()
    threading.Thread(target=loop, daemon=True).start()


_YTDLP_UPDATE_STAMP = os.path.join(BASE_DIR, ".ytdlp_update")


def _ydlp_try_upgrade_daily():
    """Раз в сутки проверяем свежую версию yt-dlp и ставим её в venv.

    Каждое скачивание бот делает свежим subprocess'ом (`.venv/bin/python
    -m yt_dlp`), поэтому обновление пакета действует на следующую попытку
    сразу, без перезапуска. Штамп — чтобы не дёргать pip чаще раза в сутки.
    """
    try:
        if os.path.isfile(_YTDLP_UPDATE_STAMP) and time.time() - os.path.getmtime(_YTDLP_UPDATE_STAMP) < 86400:
            return False
        open(_YTDLP_UPDATE_STAMP, "w").close()
    except Exception:
        pass
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "yt-dlp"],
            capture_output=True, text=True, timeout=240,
        )
        log("yt-dlp daily upgrade: rc=%d %s" % (r.returncode, (r.stderr or "").strip()[-160:]))
        return r.returncode == 0
    except Exception as e:
        log("yt-dlp daily upgrade err: %s" % str(e)[:120])
        return False


def _start_ydlp_updater():
    threading.Thread(target=_ydlp_try_upgrade_daily, daemon=True).start()


class MusicBot(TeamTalk5.TeamTalk):
    def __init__(self):
        super().__init__()
        self.api_q = queue.Queue()  # main-thread work items
        self.lock = threading.Lock()
        self.my_user_id = 0
        self.my_channel_id = 0
        self.play_channel_id = 0
        self.logged_in = False
        self.joined = False
        self.queue = []  # list of (url, title)
        self.current = None  # (url, title)
        self.current_file = None
        self.cur_source = None  # (key, label, is_radio) — what's playing now, for favorites
        self.favorites = self._load_favorites()  # list of {"key","label","radio"}
        self.playing = False
        self.downloading = set()
        self.reconnect = True
        self.connected = False
        self._last_err_sent = 0
        self.paused = False
        self.volume = min(DEFAULT_VOLUME, MAX_VOLUME)
        self.cur_vol = self.volume / 100.0  # actual gain, ramps toward self.volume
        self.service = DEFAULT_SERVICE  # "yt" = YouTube, "ym" = Yandex.Music
        self.cur_offset_ms = 0
        self.segment_started_at = 0
        self.current_orig = None
        # voice transmission (TT_InsertAudioBlock)
        self.voice_stop = threading.Event()
        self.voice_thread = None
        self.voice_proc = None
        self.voice_offset_base = 0
        self.voice_started_at = 0
        # last search result list for n/b switching and auto-advance
        self.search_results = []
        self.search_index = 0
        self.auto_list = False  # True when playing through the search-result list
        self.silent = False  # True when switching tracks on auto-advance (no status spam)
        # playlist navigation (YouTube / Yandex Music playlist links)
        self.playlist = []  # list of (url, title)
        self.playlist_index = -1
        self.auto_playlist = False  # True when playing through a playlist
        # radio stations from radio/ folder (m3u playlists grouped by category)
        self.radio = self._load_radio()
        if self.radio:
            log("radio loaded: %d stations" % len(self.radio))
        self.nickname = NICKNAME
        self.status_msg = ""
        try:
            s = open(STATUS_MSG_FILE).read().strip()
            if s:
                self.status_msg = s
        except Exception:
            pass
        # reply targeting: PM to the command author, optionally mirrored to channel
        self.reply_user_id = 0  # who sent the last command → PM replies
        self.channel_msg = self._load_channel_msg()  # mirror replies to channel (cm)
        self.voice_announce = self._load_voice_announce()  # voice-announce track titles (vo)
        self._announce_pending = None  # (path, offset) of real track waiting behind a title announcement
        # dl: upload current track to the channel as a server file (TT_DoSendFile)
        self._dl_cmd_id = None  # command id of the active upload (from TT_DoSendFile)
        self._dl_remote = None  # remote file name
        self._dl_local = None  # temp local copy to remove after upload
        # optional Telegram relay: an own bot that forwards files and commands into the channel
        self._tg_offset = 0
        self._tg_reply_chat = None  # set while handling a Telegram command → mirror replies
        self._pending_msg = None  # ожидание текста для отправки ЛС в TeamTalk: {cid, uid, nick}
        self._ready_time = None  # when the bot finished joining — for join/leave notify grace
        # подписки на уведомления: /sub в TeamTalk → ссылка → активация в Telegram
        self.users = {}  # nUserID -> User (кто сейчас на сервере)
        self._tg_username = None  # bot username from getMe, для /sub-ссылок
        self.sub_pending = {}  # token -> {nick, username, nUserID, created}
        self.sub_active = {}  # chat_id(str) -> {nick, username, nUserID, subscribed_at}
        self._load_subs()
        # подписка на музыку: отдельный Telegram-бот, присылает играющие треки (sub mus)
        self.mus_pending = {}  # token -> {nick, username, nUserID, created}
        self.music_subs = {}  # chat_id(str) -> {nick, username, nUserID, subscribed_at}
        self._load_music_subs()
        self._music_username = None  # username музыкального бота (getMe, кэш)
        self._music_offset = 0
        self.admins = self._load_admins()  # user id администраторов Telegram (users.db); владелец — всегда
        self.bans = self._load_bans()  # баны, выданные через Telegram (для /unban)
        # ---- авто-защита от ботнетов (гео-РФ для не-админ входов + ботнет-ники) ----
        self._prot_cfg = self._prot_read_cfg()
        self._ru_starts = []  # отсортированные начала RU-подсетей (bisect)
        self._ru_ends = []
        self._banned_ips = set()
        for _rec in (self.bans or {}).values():
            _bip = (_rec or {}).get("ip") or ""
            if _bip:
                self._banned_ips.add(_bip)
        self._prot_recent = deque()  # недавние входы (time, ip) — детект всплеска
        self._prot_burst_until = 0.0
        self._prot_counts = {}  # текст причины -> сколько раз (агрегированный репорт)
        self._prot_last_report = 0.0
        self._prot_lock = threading.Lock()
        self._prot_flush = None  # поток-флушер репортов
        self._prot_worker = None  # поток-банильщик (серийная очередь)
        self._prot_q = queue.Queue()
        self._prot_bad = {}  # uid -> причина: этих не анонсируем (вход/приветствие)
        self._prot_geo_off_warned = False
        if self._prot_cfg.get("enabled") and self._prot_cfg.get("geo_enabled"):
            self._prot_load_ranges()
        self.pending_replies = self._load_replies()  # двухсторонние реплики: tg message_id → данные пользователя
        self._prune_replies()
        if TG_TOKEN:
            threading.Thread(target=self._tg_poll, daemon=True, name="tg-poll").start()
            threading.Thread(target=self._tg_register_commands, daemon=True, name="tg-cmds").start()
            log("telegram relay enabled")
        if TG_MUSIC_TOKEN:
            threading.Thread(target=self._music_poll, daemon=True, name="music-poll").start()
            log("music telegram bot enabled")

    def _load_channel_msg(self):
        try:
            return open(CHANNEL_MSG_FILE).read().strip() == "1"
        except Exception:
            return DEFAULT_CHANNEL_MSG

    def _save_channel_msg(self):
        try:
            with open(CHANNEL_MSG_FILE, "w") as f:
                f.write("1" if self.channel_msg else "0")
        except Exception as e:
            log("channel_msg save err: %s" % e)

    def _load_voice_announce(self):
        try:
            return open(VOICE_ANNOUNCE_FILE).read().strip() == "1"
        except Exception:
            return False

    def _save_voice_announce(self):
        try:
            with open(VOICE_ANNOUNCE_FILE, "w") as f:
                f.write("1" if self.voice_announce else "0")
        except Exception as e:
            log("voice_announce save err: %s" % e)

    def _scale_pcm(self, chunk):
        """Apply volume with a smooth ramp toward the target (no ffmpeg restart).

        Each call is one 20 ms block; self.cur_vol steps ~2% toward self.volume,
        so a full-scale change eases in over ~1 second instead of snapping.
        """
        target = self.volume / 100.0
        cur = self.cur_vol
        if cur < target:
            cur = min(target, cur + 0.02)
        elif cur > target:
            cur = max(target, cur - 0.02)
        self.cur_vol = cur
        if cur >= 0.999:
            return chunk
        a = array.array("h")
        a.frombytes(chunk)
        for i in range(len(a)):
            a[i] = int(a[i] * cur)
        return a.tobytes()

    # ----- helpers ---------------------------------------------------
    # ----- SDK wrappers (bindings need explicit byte strings) --------
    def connect(self, host, tcp, udp, ltcp=0, ludp=0, enc=False):
        return TeamTalk5._Connect(self._tt, _b(host), tcp, udp, ltcp, ludp, enc)

    def doLogin(self, nick, user, pwd, client):
        return TeamTalk5._DoLoginEx(self._tt, _b(nick), _b(user), _b(pwd), _b(client))

    def doJoinChannelByID(self, cid, pwd):
        return TeamTalk5._DoJoinChannelByID(self._tt, cid, _b(pwd))

    def doTextMessage(self, msg):
        return TeamTalk5._DoTextMessage(self._tt, byref(msg))

    def startStreamingMediaFileToChannel(self, path, video_codec=None):
        return TeamTalk5._StartStreamingMediaFileToChannel(self._tt, _b(path), video_codec)

    def startStreamingMediaFileToChannelEx(self, path, playback, video_codec=None):
        return TeamTalk5._StartStreamingMediaFileToChannelEx(
            self._tt, _b(path), byref(playback), video_codec
        )

    def updateStreamingMediaFileToChannel(self, playback):
        return TeamTalk5._UpdateStreamingMediaFileToChannel(self._tt, byref(playback), None)

    def insertAudioBlock(self, block):
        return TeamTalk5._InsertAudioBlock(self._tt, block)

    def insertAudioBlockEnd(self):
        """End the raw-audio voice input session (lpAudioBlock=NULL)."""
        return TeamTalk5._InsertAudioBlock(self._tt, None)

    def doChangeStatus(self, status_mode, text):
        return TeamTalk5._DoChangeStatus(self._tt, status_mode, _b(text))

    def doChangeNickname(self, nick):
        return TeamTalk5._DoChangeNickname(self._tt, _b(nick))

    def getChannelIDFromPath(self, path):
        return TeamTalk5._GetChannelIDFromPath(self._tt, _b(path))

    # ----- telegram relay (own bot, forwards files into the channel) -----
    def _tg_api(self, method, **params):
        url = "https://api.telegram.org/bot%s/%s" % (TG_TOKEN, method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def _tg_poll(self):
        while True:
            try:
                res = self._tg_api("getUpdates", timeout=25, offset=self._tg_offset)
                for upd in res.get("result", []):
                    self._tg_offset = upd.get("update_id", 0) + 1
                    self._tg_handle_update(upd)
            except Exception as e:
                log("tg poll err: %s" % str(e)[:150])
                time.sleep(5)

    # ---- музыкальный Telegram-бот: подписка sub mus, раздача треков ----

    def _music_api(self, method, **params):
        url = "https://api.telegram.org/bot%s/%s" % (TG_MUSIC_TOKEN, method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())

    def _music_bot_username(self):
        if self._music_username:
            return self._music_username
        try:
            res = self._music_api("getMe")
            u = ((res or {}).get("result") or {}).get("username")
            if u:
                self._music_username = u
        except Exception as e:
            log("music getMe err: %s" % str(e)[:100])
        return self._music_username

    def _music_send_text(self, chat_id, text):
        try:
            self._music_api("sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception as e:
            log("music send err: %s" % str(e)[:120])

    def _music_send_document(self, chat_id, path, caption="", fname=None):
        """Отправить файл подписчику музыки. multipart собирается вручную — в venv нет requests."""
        try:
            boundary = "----BotBoundary" + uuid.uuid4().hex
            fname = fname or os.path.basename(path)
            with open(path, "rb") as f:
                fdata = f.read()
            def _part(name, value):
                return ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                        % (boundary, name, value)).encode("utf-8")
            body = (
                _part("chat_id", str(chat_id))
                + _part("caption", caption)
                + ("--%s\r\nContent-Disposition: form-data; name=\"document\"; filename=\"%s\"\r\n"
                   "Content-Type: application/octet-stream\r\n\r\n" % (boundary, fname)).encode("utf-8")
                + fdata
                + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
            )
            url = "https://api.telegram.org/bot%s/sendDocument" % TG_MUSIC_TOKEN
            req = urllib.request.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            log("music send doc err: %s" % str(e)[:150])
            return None

    def _music_poll(self):
        while True:
            try:
                res = self._music_api("getUpdates", timeout=25, offset=self._music_offset)
                for upd in res.get("result", []):
                    self._music_offset = upd.get("update_id", 0) + 1
                    self._music_handle_update(upd)
            except Exception as e:
                log("music poll err: %s" % str(e)[:150])
                time.sleep(5)

    def _music_handle_update(self, upd):
        msg = upd.get("message")
        if not msg:
            return
        cid = str((msg.get("chat") or {}).get("id") or "")
        if not cid:
            return
        text = (msg.get("text") or "").strip()
        low = text.lower()
        if low in ("/unsub_mus", "/unsub", "/stop"):
            if cid in self.music_subs:
                del self.music_subs[cid]
                self._save_music_subs()
                self._music_send_text(int(cid), "Отписался от музыки.")
            else:
                self._music_send_text(int(cid), "Ты и так не подписан на музыку.")
            return
        # /start sub_mus_<token> — активация подписки по ссылке
        token = text[len("/start "):].strip() if low.startswith("/start ") else ""
        if token.startswith("sub_mus_"):
            rec = self.mus_pending.pop(token, None)
            self._save_music_subs()
            if not rec:
                self._music_send_text(int(cid), "Ссылка недействительна или истекла. Отправь sub mus заново в TeamTalk.")
                return
            rec["subscribed_at"] = time.time()
            self.music_subs[cid] = rec
            self._save_music_subs()
            who = rec.get("nick") or rec.get("username") or "твой аккаунт"
            self._music_send_text(int(cid), "✅ Подписан на музыку (%s). Каждый сыгранный трек буду присылать сюда. Отписаться — /unsub_mus." % who)
            return
        if cid in self.music_subs:
            self._music_send_text(int(cid), "Ты подписан на музыку. Треки приходят сюда. Отписаться — /unsub_mus.")
        else:
            self._music_send_text(int(cid), "Это бот для подписки на музыку. На сервере TeamTalk отправь боту личное сообщение «sub mus» — получишь ссылку на подписку.")

    def _music_broadcast(self, path, title):
        """Разослать играющий трек всем подписчикам музыки (в фоне, не блокируя музыку)."""
        if not TG_MUSIC_TOKEN or not self.music_subs:
            return
        if not path or not os.path.isfile(path):
            return  # радио и треки без локального файла не шлём
        title = title or "трек"
        ext = os.path.splitext(path)[1] or ".mp3"
        safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:80] or "track"
        fname = safe + ext
        subs = list(self.music_subs.keys())

        def _worker():
            for cid in subs:
                try:
                    self._music_send_document(int(cid), path, title[:100], fname)
                except Exception as e:
                    log("music broadcast err: %s" % str(e)[:120])
        threading.Thread(target=_worker, daemon=True, name="music-broadcast").start()

    def _load_music_subs(self):
        try:
            with open(MUSIC_SUBS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            now = time.time()
            self.mus_pending = {
                t: p for t, p in (d.get("pending") or {}).items()
                if p.get("created", 0) > now - MUSIC_SUB_TTL_SEC
            }
            self.music_subs = {str(c): s for c, s in (d.get("active") or {}).items()}
        except Exception:
            self.mus_pending = {}
            self.music_subs = {}

    def _save_music_subs(self):
        try:
            now = time.time()
            self.mus_pending = {
                t: p for t, p in self.mus_pending.items()
                if p.get("created", 0) > now - MUSIC_SUB_TTL_SEC
            }
            with open(MUSIC_SUBS_FILE, "w", encoding="utf-8") as f:
                json.dump({"pending": self.mus_pending, "active": self.music_subs},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("music subs save err: %s" % e)

    def _tg_allowed(self, msg):
        """Владелец из конфига и админы из users.db могут слать команды."""
        if not TG_OWNER_USER_ID and not self.admins:
            return True
        ids = self._tg_admin_ids()
        uid = (msg.get("from") or {}).get("id")
        cid = (msg.get("chat") or {}).get("id")
        return uid in ids or cid in ids

    def _tg_handle_update(self, upd):
        cq = upd.get("callback_query")
        if cq:
            self._tg_handle_callback(cq)
            return
        msg = upd.get("message") or upd.get("channel_post")
        if not msg:
            return
        text = (msg.get("text") or "").strip()
        if text:
            low = text.lower()
            # подписка по deep-link: любой пользователь, не только владелец
            if low.startswith("/start") or low == "/unsub":
                self._tg_handle_sub_msg(msg, text)
                return
            if low in ("/help", "help"):
                self._tg_handle_help(msg)
                return
            if low in ("/online", "online"):
                text, kb = self._online_view()
                if kb:
                    self._tg_send_kb((msg.get("chat") or {}).get("id"), text, kb)
                else:
                    self._tg_send_text((msg.get("chat") or {}).get("id"), text)
                return
            # treat text as a bot command; mirror replies back to this chat
            if not self._tg_allowed(msg):
                return
            cid = (msg.get("chat") or {}).get("id")
            # владелец прислал экспорт кук YouTube текстом — сохранить как файл кук
            if _looks_like_cookie_export(text):
                self._tg_install_yt_cookies(msg, text)
                return
            # --- ответ на пересланное ЛС из TeamTalk: двухсторонние реплики ---
            self._prune_replies()
            rmid = str((msg.get("reply_to_message") or {}).get("message_id") or "")
            if rmid and rmid in self.pending_replies:
                rec = self.pending_replies[rmid]
                if self._send_to_tt_user(rec.get("tt_user_id") or 0, text):
                    rec["last_used_at"] = time.time()
                    self._save_replies()
                    self._tg_send_text(cid, "Отправил %s в TeamTalk." % (rec.get("nick") or "сообщение"))
                else:
                    self._tg_send_text(cid, "Не отправилось: пользователь офлайн или ЛС недоступно.")
                return
            # ожидание текста для отправки личного сообщения пользователю (из /online)
            if self._pending_msg and self._pending_msg.get("cid") == cid:
                if text.startswith("/"):
                    self._pending_msg = None
                else:
                    pm = self._pending_msg
                    self._pending_msg = None
                    if self._send_to_tt_user(pm["uid"], text):
                        self._tg_send_text(cid, "Отправил %s в TeamTalk." % (pm.get("nick") or pm["uid"]))
                    else:
                        self._tg_send_text(cid, "Не отправилось: пользователь офлайн или ЛС недоступно.")
                    return
            if low in ("/admins", "admins"):
                self._tg_send_text(cid, self._admins_text())
                return
            if low.startswith("/admin ") or low == "/admin":
                self._tg_admin_cmd(msg, text)
                return
            if low.startswith("/unadmin ") or low == "/unadmin":
                self._tg_unadmin_cmd(msg, text)
                return
            if low in ("/subs", "subs"):
                text, kb = self._subs_list_view()
                self._tg_send_kb(cid, text, kb)
                return
            if low.startswith("/delsub ") or low == "/delsub":
                self._tg_delsub_cmd(msg, text)
                return
            if low in ("/kick", "kick"):
                text, kb = self._kick_ban_view("kick")
                self._tg_send_kb(cid, text, kb)
                return
            if low in ("/ban", "ban"):
                text, kb = self._kick_ban_view("ban")
                self._tg_send_kb(cid, text, kb)
                return
            if low in ("/unban", "unban"):
                text, kb = self._unban_view()
                self._tg_send_kb(cid, text, kb)
                return
            if low.startswith("/net ") or low == "/net" or low.startswith("/broadcast ") or low == "/broadcast":
                body = text.split(" ", 1)[1].strip() if " " in text else ""
                if not body:
                    self._tg_send_text(cid, "Формат: /net <текст> — сетевое сообщение всем на сервере.")
                elif self._send_network_msg(body):
                    self._tg_send_text(cid, "Сетевое сообщение отправлено всем на сервере.")
                else:
                    self._tg_send_text(cid, "Не отправилось: у учётки бота нет права на сетевые сообщения.")
                return
            prev = self.reply_user_id
            self.reply_user_id = 0
            self._tg_reply_chat = (msg.get("chat") or {}).get("id")
            try:
                self._announce_tg_cmd(msg, text)
                self._handle_cmd(text, 0)
            except Exception as e:
                log("tg cmd err: %s" % e)
            finally:
                self.reply_user_id = prev
                self._tg_reply_chat = None
            return
        media = msg.get("voice") or msg.get("audio") or msg.get("video") or msg.get("document")
        if not media:
            return
        file_id = media.get("file_id")
        if not file_id:
            return
        # документ-файл с экспортом кук (например, .txt из Get cookies.txt) — принять как вход
        if "document" in media:
            path = self._tg_download(file_id)
            if path:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read(200000)
                except Exception:
                    content = ""
                if _looks_like_cookie_export(content):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    self._tg_install_yt_cookies(msg, content)
                    return
                self.api_q.put(("local_file", path, (media.get("file_name") or "audio")[:80]))
            return
        title = (
            msg.get("caption")
            or media.get("file_name")
            or media.get("title")
            or "audio"
        ).strip() or "audio"
        path = self._tg_download(file_id)
        if path:
            self.api_q.put(("local_file", path, title[:80]))

    def _tg_install_yt_cookies(self, msg, text):
        """Сохранить присланный владельцем экспорт кук YouTube в файл кук."""
        cid = (msg.get("chat") or {}).get("id")
        if not COOKIES:
            self._tg_send_text(cid, "Путь к кукам не задан (services.yt.cookiefile_path) — некуда сохранять.")
            return
        try:
            body = _normalize_cookie_text(text)
            from http.cookiejar import MozillaCookieJar
            tmp = COOKIES + ".new"
            with open(tmp, "w") as fh:
                fh.write(body)
            try:
                cj = MozillaCookieJar(tmp)
                cj.load(ignore_discard=True, ignore_expires=True)
                names = {c.name for c in cj}
            except Exception:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                self._tg_send_text(cid, "Не получилось разобрать куки. Нужен экспорт из браузера в формате Netscape (напр. расширением Get cookies.txt).")
                return
            os.replace(tmp, COOKIES)
            try:
                os.chmod(COOKIES, 0o600)
            except Exception:
                pass
            logged = "LOGIN_INFO" in names
            self._tg_send_text(
                cid,
                "Сохранил куки: %d шт., вход в аккаунт %s.\n"
                "Правда, Google с серверного адреса такие сессии часто гасит — "
                "возрастное видео может всё равно не пустить."
                % (len(cj), "есть" if logged else "не вижу")
            )
        except Exception as e:
            self._tg_send_text(cid, "Не получилось сохранить куки: %s" % str(e)[:120])

    def _cmd_login(self):
        """Статус входа в YouTube + как войти своим аккаунтом."""
        if not COOKIES or not os.path.isfile(COOKIES):
            self._send(
                "В YouTube сейчас не залогинен.\n"
                "Как войти: открой youtube.com в обычном браузере, зайди в аккаунт, "
                "экспортируй куки расширением Get cookies.txt (формат Netscape) и пришли "
                "весь текст боту в Telegram — сам сохраню и проверю."
            )
            return
        try:
            from http.cookiejar import MozillaCookieJar
            cj = MozillaCookieJar(COOKIES)
            cj.load(ignore_discard=True, ignore_expires=True)
            names = {c.name for c in cj}
        except Exception as e:
            self._send("Файл кук есть, но читается с ошибкой: %s" % str(e)[:120])
            return
        logged = "LOGIN_INFO" in names
        if logged:
            self._send(
                "Вход в YouTube есть: куки на месте (%d шт.).\n"
                "Обновить — пришли свежий экспорт кук в Telegram. Если Google начал "
                "просить войти, сессию с серверного адреса скорее всего погасил он."
                % len(cj)
            )
        else:
            self._send(
                "Куки на месте (%d шт.), но входа в аккаунт в них нет — Google их гасит. "
                "Обнови: открой youtube.com в браузере и пришли свежий экспорт кук в Telegram."
                % len(cj)
            )

    def _tg_send_text(self, chat_id, text):
        try:
            self._tg_api("sendMessage", chat_id=chat_id, text=text[:4000])
        except Exception as e:
            log("tg send err: %s" % str(e)[:120])

    # ---- inline-клавиатуры: интерактивное управление подписчиками ----

    def _tg_send_kb(self, chat_id, text, kb):
        try:
            self._tg_api("sendMessage", chat_id=chat_id, text=text[:4000],
                         reply_markup=json.dumps({"inline_keyboard": kb}))
        except Exception as e:
            log("tg send_kb err: %s" % str(e)[:120])

    def _tg_edit_kb(self, chat_id, message_id, text, kb):
        try:
            self._tg_api("editMessageText", chat_id=chat_id, message_id=message_id,
                         text=text[:4000],
                         reply_markup=json.dumps({"inline_keyboard": kb}))
        except Exception as e:
            log("tg edit_kb err: %s" % str(e)[:120])

    def _tg_answer_cb(self, qid, text="", alert=False):
        try:
            self._tg_api("answerCallbackQuery", callback_query_id=qid,
                         text=text[:200], show_alert=alert)
        except Exception as e:
            log("tg answer_cb err: %s" % str(e)[:120])

    def _tg_cb_allowed(self, cq):
        ids = self._tg_admin_ids()
        uid = (cq.get("from") or {}).get("id")
        cid = (cq.get("message") or {}).get("chat", {}).get("id")
        return uid in ids or cid in ids

    def _subs_list_view(self):
        """Текст и кнопки списка подписчиков: по кнопке на подписчика."""
        items = sorted(self.sub_active.items(), key=lambda kv: str(kv[0]))
        lines = ["Подписчики (%d):" % len(items)]
        kb = []
        for cid, rec in items:
            name = (rec.get("tg_name") or rec.get("tg_username")
                    or rec.get("nick") or rec.get("username") or "id %s" % cid)
            kb.append([{"text": name, "callback_data": "subs:view:%s" % cid}])
        if items:
            lines.append("Нажми на подписчика — откроются действия.")
        else:
            lines.append("Пока никто не подписан.")
        return "\n".join(lines), kb

    def _subs_view(self, cid, actor_uid):
        """Карточка подписчика: статус и кнопки действий."""
        rec = self.sub_active.get(str(cid)) or {}
        name = (rec.get("tg_name") or rec.get("tg_username")
                or rec.get("nick") or rec.get("username") or "id %s" % cid)
        is_admin = int(cid) in self.admins
        is_owner = str(cid) == str(TG_OWNER_USER_ID)
        lines = [
            "Подписчик: %s" % name,
            "ID: %s" % cid,
        ]
        if rec.get("subscribed_at"):
            lines.append("Подписан: %s" % time.strftime("%d.%m %H:%M",
                        time.localtime(rec["subscribed_at"])))
        if is_owner:
            lines.append("Статус: владелец")
        elif is_admin:
            lines.append("Статус: админ")
        else:
            lines.append("Статус: подписчик")
        kb = []
        if is_owner:
            lines.append("Владельца нельзя отписать или снять с админов.")
        else:
            if actor_uid == TG_OWNER_USER_ID:
                if is_admin:
                    kb.append([{"text": "Убрать из админов",
                                "callback_data": "subs:unadmin:%s" % cid}])
                else:
                    kb.append([{"text": "Назначить админом",
                                "callback_data": "subs:makeadmin:%s" % cid}])
            kb.append([{"text": "Отписать от уведомлений",
                        "callback_data": "subs:del:%s" % cid}])
        kb.append([{"text": "Назад к списку", "callback_data": "subs:list"}])
        return "\n".join(lines), kb

    def _online_users_for_moderation(self):
        """(uid, nick, username, ip) по онлайн-пользователям, кроме бота и админов."""
        out = []
        for uid, u in list(self.users.items()):
            if uid == self.my_user_id:
                continue
            utype = int(getattr(u, "uUserType", 0) or 0)
            if utype & 2:  # админов кикать/банить нельзя (USERTYPE_ADMIN — бит)
                continue
            nick = self._tt_field(u, "szNickname") or self._tt_field(u, "szUsername")
            out.append((uid, nick, self._tt_field(u, "szUsername"),
                        self._tt_field(u, "szIPAddress")))
        out.sort(key=lambda r: r[1].lower())
        return out

    def _kick_ban_view(self, mode):
        """Список онлайн-пользователей с кнопками «кикнуть»/«забанить»."""
        users = self._online_users_for_moderation()
        verb = "кикнуть" if mode == "kick" else "забанить"
        if not users:
            return "На сервере сейчас некого %s — все админы или никого нет." % verb, []
        lines = ["Кого %s? (%d на сервере)" % (verb, len(users))]
        kb = []
        for uid, nick, username, ip in users:
            label = "%s (id %s)" % (nick or username or uid, uid)
            kb.append([{"text": label, "callback_data": "%s:do:%s" % (mode, uid)}])
        return "\n".join(lines), kb

    def _online_view(self):
        """Кто на сервере — кнопки по пользователям (кроме бота). Нажатие открывает действия."""
        users = []
        for uid, u in list(self.users.items()):
            if uid == self.my_user_id:
                continue
            nick = self._tt_field(u, "szNickname") or self._tt_field(u, "szUsername")
            users.append((uid, nick, u))
        users.sort(key=lambda r: (r[1] or "").lower())
        server = self._server_name()
        if not users:
            return "Сейчас на сервере «%s» никого, кроме меня." % server, []
        lines = ["Сейчас на сервере «%s» (%d):" % (server, len(users)),
                 "Нажми на пользователя — действия."]
        kb = []
        for uid, nick, u in users:
            kb.append([{"text": nick or "id %s" % uid, "callback_data": "online:view:%s" % uid}])
        return "\n".join(lines), kb

    def _user_view(self, uid):
        """Карточка онлайн-пользователя: инфо + действия (ЛС, кик, бан)."""
        u = self.users.get(uid)
        if not u:
            return "Пользователь уже покинул сервер.", []
        nick = self._tt_field(u, "szNickname") or self._tt_field(u, "szUsername") or "id %s" % uid
        uname = self._tt_field(u, "szUsername")
        ip = self._tt_field(u, "szIPAddress")
        status = self._tt_field(u, "szStatusMsg")
        lines = ["Пользователь: %s" % nick]
        if uname:
            lines.append("Username: %s" % uname)
        if status:
            lines.append("Статус: %s" % status[:50])
        if ip:
            lines.append("IP: %s" % ip)
        kb = [[{"text": "✉ Написать ЛС", "callback_data": "online:msg:%s" % uid}]]
        if not (int(getattr(u, "uUserType", 0) or 0) & 2):  # админов кикать/банить нельзя
            kb.append([{"text": "Кикнуть", "callback_data": "kick:do:%s" % uid},
                       {"text": "Забанить", "callback_data": "ban:do:%s" % uid}])
        kb.append([{"text": "← Назад", "callback_data": "online:list"}])
        return "\n".join(lines), kb

    def _unban_view(self):
        """Список записей банов из bans.json с кнопками разбана."""
        if not self.bans:
            return "В базе банов пусто — ботом никто не банен.", []
        lines = ["Забаненные (%d):" % len(self.bans)]
        kb = []
        for uid, rec in sorted(self.bans.items()):
            nick = rec.get("nick") or "id %s" % uid
            lines.append("%s (id %s)" % (nick, uid))
            kb.append([{"text": "Разбанить: %s (id %s)" % (nick, uid),
                        "callback_data": "unban:do:%s" % uid}])
        return "\n".join(lines), kb

    def _tg_handle_callback(self, cq):
        data = cq.get("data") or ""
        qid = cq.get("id")
        actor = (cq.get("from") or {}).get("id")
        cid = (cq.get("message") or {}).get("chat", {}).get("id")
        mid = (cq.get("message") or {}).get("message_id")
        if not self._tg_cb_allowed(cq):
            self._tg_answer_cb(qid, "Нет доступа.", alert=True)
            return
        if data == "subs:list":
            self._tg_answer_cb(qid)
            text, kb = self._subs_list_view()
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("subs:view:"):
            self._tg_answer_cb(qid)
            target = data.split(":", 2)[2]
            text, kb = self._subs_view(target, actor)
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("subs:makeadmin:"):
            if actor != TG_OWNER_USER_ID:
                self._tg_answer_cb(qid, "Назначать админов может только владелец.", alert=True)
                return
            target = data.split(":", 2)[2]
            if str(target) == str(TG_OWNER_USER_ID):
                self._tg_answer_cb(qid, "Владелец и так админ.", alert=True)
                return
            if int(target) in self.admins:
                self._tg_answer_cb(qid, "Уже админ.", alert=True)
                return
            self.admins.append(int(target))
            self._save_admins()
            self._tg_register_commands()
            self._tg_notify_promoted(int(target))
            self._tg_answer_cb(qid, "Админ назначен.")
            text, kb = self._subs_view(target, actor)
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("subs:unadmin:"):
            if actor != TG_OWNER_USER_ID:
                self._tg_answer_cb(qid, "Снимать админов может только владелец.", alert=True)
                return
            target = data.split(":", 2)[2]
            if str(target) == str(TG_OWNER_USER_ID):
                self._tg_answer_cb(qid, "Владельца нельзя снять.", alert=True)
                return
            if int(target) in self.admins:
                self.admins.remove(int(target))
                self._save_admins()
                self._tg_register_commands()
                self._tg_answer_cb(qid, "Админ снят.")
            else:
                self._tg_answer_cb(qid, "Не админ.", alert=True)
            text, kb = self._subs_view(target, actor)
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("subs:del:"):
            target = data.split(":", 2)[2]
            if str(target) == str(TG_OWNER_USER_ID):
                self._tg_answer_cb(qid, "Владельца нельзя отписать.", alert=True)
                return
            if str(target) in self.sub_active:
                del self.sub_active[str(target)]
                self._save_subs()
                self._tg_answer_cb(qid, "Подписка убрана.")
                text, kb = self._subs_list_view()
                self._tg_edit_kb(cid, mid, text, kb)
            else:
                self._tg_answer_cb(qid, "Не подписчик.", alert=True)
            return
        if data == "online:list":
            self._tg_answer_cb(qid)
            text, kb = self._online_view()
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("online:view:"):
            self._tg_answer_cb(qid)
            uid = int(data.split(":", 2)[2])
            text, kb = self._user_view(uid)
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("online:msg:"):
            uid = int(data.split(":", 2)[2])
            u = self.users.get(uid)
            nick = (self._tt_field(u, "szNickname") or self._tt_field(u, "szUsername")
                    or "id %s" % uid) if u else "id %s" % uid
            self._pending_msg = {"cid": cid, "uid": uid, "nick": nick}
            self._tg_answer_cb(qid, "Напиши текст сообщения.")
            self._tg_send_text(cid, "Напиши текст для %s — отправлю ему в TeamTalk "
                                      "личным сообщением. Отмена — любая команда со слэшем." % nick)
            return
        if data.startswith("kick:do:"):
            target = int(data.split(":", 2)[2])
            u = self.users.get(target)
            nick = self._tt_field(u, "szNickname") if u else "id %s" % target
            try:
                self.doKickUser(target, 0)
                self._tg_answer_cb(qid, "Кикнут: %s" % nick)
            except Exception as e:
                log("kick err: %s" % e)
                self._tg_answer_cb(qid, "Не удалось кикнуть.", alert=True)
            text, kb = self._kick_ban_view("kick")
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("ban:do:"):
            target = int(data.split(":", 2)[2])
            u = self.users.get(target)
            nick = self._tt_field(u, "szNickname") if u else "id %s" % target
            ip = self._tt_field(u, "szIPAddress") if u else ""
            try:
                # кикаем серверно (канал 0 = весь сервер) и банем IP, чтобы не вернулся
                self.doKickUser(target, 0)
                if ip:
                    self.doBanIPAddress(_b(ip), 0)
                    self._banned_ips.add(ip)
                    self.bans[str(target)] = {
                        "nick": nick,
                        "username": self._tt_field(u, "szUsername") if u else "",
                        "ip": ip,
                        "banned_at": time.time(),
                    }
                    self._save_bans()
                    self._tg_answer_cb(qid, "Забанен: %s" % nick)
                else:
                    self._tg_answer_cb(qid, "Кикнут: %s (IP не виден — серверный бан не выдан)" % nick)
            except Exception as e:
                log("ban err: %s" % e)
                self._tg_answer_cb(qid, "Не удалось забанить.", alert=True)
            text, kb = self._kick_ban_view("ban")
            self._tg_edit_kb(cid, mid, text, kb)
            return
        if data.startswith("unban:do:"):
            target = data.split(":", 2)[2]
            rec = self.bans.get(target) or {}
            nick = rec.get("nick") or "id %s" % target
            ok = False
            ip = rec.get("ip") or ""
            try:
                if ip:
                    self.doUnBanUser(_b(ip), 0)
                    self._banned_ips.discard(ip)
                    ok = True
            except Exception as e:
                log("unban err: %s" % e)
            self.bans.pop(target, None)
            self._save_bans()
            self._tg_answer_cb(qid,
                "Разбанен: %s" % nick if ok else "Запись удалена (IP не было — серверный бан не снимался).")
            text, kb = self._unban_view()
            self._tg_edit_kb(cid, mid, text, kb)
            return
        log("tg unknown callback: %s" % data)

    def _tg_handle_sub_msg(self, msg, text):
        """/start (по deep-link sub_<token>) и /unsub — для подписчиков, не только владельца."""
        cid = (msg.get("chat") or {}).get("id")
        if not cid:
            return
        # запоминаем, как человек записан в Telegram (first_name + last_name) —
        # показываем это имя в списке подписчиков вместо user ID
        fr = msg.get("from") or {}
        tg_name = " ".join(filter(None, [fr.get("first_name") or "", fr.get("last_name") or ""])).strip()
        tg_uname = fr.get("username") or ""
        if str(cid) in self.sub_active:
            rec = self.sub_active[str(cid)]
            changed = False
            if tg_name and rec.get("tg_name") != tg_name:
                rec["tg_name"] = tg_name
                changed = True
            if tg_uname and rec.get("tg_username") != tg_uname:
                rec["tg_username"] = tg_uname
                changed = True
            if changed:
                self._save_subs()
        low = text.lower()
        if low == "/unsub":
            if str(cid) in self.sub_active:
                del self.sub_active[str(cid)]
                self._save_subs()
                self._tg_send_text(cid, "Отписался от уведомлений о входе/выходе.")
            else:
                self._tg_send_text(cid, "Ты и так не подписан.")
            return
        # /start или /start sub_<token>
        token = text[len("/start "):].strip() if low.startswith("/start ") else ""
        if not token.startswith("sub_"):
            self._tg_send_text(cid, "Я музыкант и оповещатель TeamTalk. На сервере отправь боту /sub — получишь ссылку на подписку.")
            return
        rec = self.sub_pending.pop(token, None)
        self._save_subs()
        if not rec:
            self._tg_send_text(cid, "Ссылка недействительна или истекла. Отправь /sub заново на сервере.")
            return
        rec["subscribed_at"] = time.time()
        if tg_name:
            rec["tg_name"] = tg_name
        if tg_uname:
            rec["tg_username"] = tg_uname
        self.sub_active[str(cid)] = rec
        self._save_subs()
        who = rec.get("nick") or rec.get("username") or "твой аккаунт"
        self._tg_send_text(cid, "✅ Подписка активна (%s): будешь получать уведомления о входе/выходе на сервере «%s». Отписаться — /unsub." % (who, self._server_name()))

    def _tg_help_text(self, is_admin=False):
        text = (
            "Команды бота (работают и без слэша):\n"
            "play <запрос или ссылка> — найти и играть; голый play — пауза/продолжить\n"
            "ссылку можно просто вставить боту — тоже сыграет\n"
            "n — следующий, b — предыдущий (по списку или плейлисту)\n"
            "pause / resume — пауза и продолжить\n"
            "stop — стоп, skip — пропустить трек\n"
            "u <ссылка> — играть по ссылке напрямую\n"
            "v <1-100> — громкость, sf <сек> — перемотка (sf -5 — назад, sb <сек> — назад)\n"
            "playlist — список плейлиста постранично\n"
            "radio — радиостанции (radio <номер> — запуск)\n"
            "fav — избранное (f +, f + <ссылка>, f <номер>, f - <номер>)\n"
            "sv yt / sv ym — сервис\n"
            "cm — отвечать в канал/личку\n"
            "cn <ник> — ник, cs <текст> — статус, sc — сохранить\n"
            "status — что сейчас играет\n"
            "sub / sub mus — подписки (команды работают в TeamTalk)\n"
            "Музыку заказывает любой, управление ботом — только админам."
        )
        if is_admin:
            text += ("\nДля админов: /admins, /subs, /kick, /ban, /unban. "
                      "Админов и подписки можно менять в /subs (кнопки).")
        return text

    def _tg_is_admin_msg(self, msg):
        ids = self._tg_admin_ids()
        uid = (msg.get("from") or {}).get("id")
        cid = (msg.get("chat") or {}).get("id")
        return uid in ids or cid in ids

    def _tg_handle_help(self, msg):
        self._tg_send_text((msg.get("chat") or {}).get("id"),
                           self._tg_help_text(self._tg_is_admin_msg(msg)))

    def _tg_register_commands(self):
        """Меню команд бота: не-админам — только общие, админам — плюс админские
        (scope в Telegram позволяет раздать меню по чатам)."""
        public = [
            {"command": "help", "description": "Список команд"},
            {"command": "start", "description": "Подписка по ссылке / старт"},
            {"command": "play", "description": "Поиск и игра: /play <запрос>"},
            {"command": "online", "description": "Кто сейчас на сервере"},
            {"command": "next", "description": "Следующий трек"},
            {"command": "prev", "description": "Предыдущий трек"},
            {"command": "volume", "description": "Громкость: /volume <1-100>"},
            {"command": "favorites", "description": "Избранное: /favorites"},
            {"command": "radio", "description": "Радиостанции"},
        ]
        # /admin, /unadmin, /delsub, /unsub намеренно НЕ в меню: назначение/снятие
        # админа и отписка делаются в списке подписчиков /subs (кнопки). По тексту
        # команды продолжают работать.
        admin = [
            {"command": "admins", "description": "Список админов бота"},
            {"command": "subs", "description": "Подписчики: админы, отписка"},
            {"command": "kick", "description": "Кикнуть пользователя"},
            {"command": "ban", "description": "Забанить пользователя"},
            {"command": "unban", "description": "Разбанить: /unban"},
            {"command": "net", "description": "Сетевое сообщение всем: /net <текст>"},
        ]
        try:
            # общее меню по умолчанию — без админских команд
            self._tg_api("setMyCommands", commands=json.dumps(public))
            # админам — полное меню в личных чатах
            for aid in self._tg_admin_ids():
                self._tg_api("setMyCommands", commands=json.dumps(public + admin),
                             scope=json.dumps({"type": "chat", "chat_id": aid}))
            log("tg commands registered (%d admin scopes)" % len(self._tg_admin_ids()))
        except Exception as e:
            log("tg setMyCommands err: %s" % str(e)[:120])

    def _tg_download(self, file_id):
        try:
            info = self._tg_api("getFile", file_id=file_id).get("result")
            if not info or not info.get("file_path"):
                return None
            fpath = info["file_path"]
            url = "https://api.telegram.org/file/bot%s/%s" % (TG_TOKEN, fpath)
            with urllib.request.urlopen(url, timeout=180) as r:
                data = r.read()
            name = os.path.basename(fpath) or ("audio_%s.ogg" % file_id[:8])
            local = os.path.join(INBOX_DIR, name)
            with open(local, "wb") as f:
                f.write(data)
            log("tg downloaded %s (%d bytes)" % (local, len(data)))
            return local
        except Exception as e:
            log("tg download err: %s" % str(e)[:150])
            return None

    def _send(self, text):
        if not self.logged_in:
            log("_send skip: logged_in=%s" % self.logged_in)
            return
        try:
            sent_pm = False
            if self.reply_user_id:
                msgs = buildTextMessage(
                    text, TextMsgType.MSGTYPE_USER, nToUserID=self.reply_user_id,
                    nChannelID=0, nFromUserID=self.my_user_id,
                )
                for m in msgs:
                    self.doTextMessage(m)
                sent_pm = True
            to_channel = bool(self.channel_msg) or not self.reply_user_id
            if to_channel and self.my_channel_id:
                msgs = buildTextMessage(
                    text, TextMsgType.MSGTYPE_CHANNEL, nChannelID=self.my_channel_id
                )
                for m in msgs:
                    self.doTextMessage(m)
            log("_send(%r) pm=%s chan=%s" % (text, sent_pm, to_channel and self.my_channel_id > 0))
        except Exception as e:
            log("send error: %s" % e)
        if self._tg_reply_chat:
            try:
                self._tg_api("sendMessage", chat_id=self._tg_reply_chat, text=text[:4000])
            except Exception as e:
                log("tg send err: %s" % str(e)[:120])

    def _enqueue_next(self):
        """Start next queue item from main thread."""
        if self.playing or not self.queue:
            return
        url, title = self.queue[0]
        self._start_download(url, title)

    def _start_download(self, url, title):
        if url in self.downloading:
            return
        self.downloading.add(url)
        t = threading.Thread(target=self._download_worker, args=(url, title), daemon=True)
        t.start()

    def _run_ydl(self, cmd, timeout=600):
        """Run yt-dlp, kill the whole process group on timeout (children may be node/js)."""
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except Exception:
                proc.kill()
            proc.wait()
            raise

    _YT_ALT_CLIENTS = ("android", "ios", "tv_embedded", "web_embedded", "mweb", "web_safari")

    def _yt_alt_download(self, out, real_url):
        """yt-dlp не взял видео основным клиентом — перебираем запасные клиенты
        YouTube (player_client), с куками и анонимно. Возвращает (rc, stdout,
        stderr) первого успеха либо (None, "", последняя ошибка)."""
        last_err = ""
        for p in glob.glob(out + ".*"):  # подчистить хвосты прошлых попыток
            try:
                os.unlink(p)
            except Exception:
                pass
        for client in self._YT_ALT_CLIENTS:
            for use_ck in (True, False):
                cmd = list(YTDLP) + [
                    "--no-playlist", "--no-simulate",
                    # Так же без mp3-транскода, как и основной клиент
                    "-f", "ba/b",
                    "--remote-components", "ejs:github",
                    "--extractor-args", "youtube:player_client=%s" % client,
                    "-o", out + ".%(ext)s",
                    "--print", "%(title)s",
                ]
                if YT_JS_RUNTIME:
                    cmd += ["--js-runtimes", "deno:%s" % YT_JS_RUNTIME]
                ck = None
                if use_ck:
                    ck = _fresh_cookies(COOKIES)
                    if ck:
                        cmd += ["--cookies", ck]
                cmd += ["--", real_url]
                try:
                    rc, stdout, stderr = self._run_ydl(cmd, timeout=300)
                except subprocess.TimeoutExpired:
                    rc, stdout, stderr = 1, "", "timeout"
                finally:
                    if ck:
                        _drop_cookies(ck)
                if rc == 0 and _find_downloaded(out):
                    log("yt alt client %s ck=%d ok" % (client, use_ck))
                    return rc, stdout, stderr
                tail = ""
                blob = (stderr or stdout or "").strip()
                if blob:
                    tail = blob.splitlines()[-1][:200]
                log("yt alt client %s ck=%d fail: %s" % (client, use_ck, tail))
                if tail:
                    last_err = tail
        return None, "", last_err

    def _ym_search_list(self, query):
        """Search Yandex Music; return list of {"key","title"} (up to 10)."""
        if not YM_TOKEN:
            return []
        try:
            from yandex_music import Client
        except Exception as e:
            log("ym import err: %s" % e)
            return []
        try:
            client = Client(YM_TOKEN).init()
            res = client.search(query, type_="track", page=0)
            if not res or not res.tracks or not res.tracks.results:
                return []
            items = []
            for tr in res.tracks.results:
                title = " - ".join(tr.artists_name()) + " - " + tr.title
                items.append({"key": "ymtrack:%s" % tr.track_id, "title": title})
                if len(items) >= 10:
                    break
            return items
        except Exception as e:
            log("ym search err: %s" % e)
            return []

    def _ym_resolve(self, track_id):
        """Resolve a Yandex track id to (direct mp3 url, display title) or (None, err)."""
        try:
            from yandex_music import Client
            client = Client(YM_TOKEN).init()
            tr = client.tracks(track_id)[0]
            title = " - ".join(tr.artists_name()) + " - " + tr.title
            info = tr.get_download_info(get_direct_links=True)
            mp3 = [i for i in info if i.codec == "mp3"]
            if not mp3:
                return None, "У трека нет mp3-версии."
            best = sorted(mp3, key=lambda x: x.bitrate_in_kbps, reverse=True)[0]
            if not best.direct_link:
                return None, "Нет прямой ссылки на файл."
            return best.direct_link, title
        except Exception as e:
            return None, "Яндекс.Музыка: %s" % str(e)[:150]

    def _yt_search_list(self, query):
        """Return list of {"key","title"} for the first *video* results, skipping
        channels/playlists that a bare ytsearch would otherwise dive into."""
        cmd = list(YTDLP) + [
            "--flat-playlist",
            "--playlist-items", "1-10",
            "--no-warnings",
            "--print", "%(title)s\t%(url)s",
        ]
        if YT_PO_TOKEN:
            cmd += ["--extractor-args", YT_PO_TOKEN]
        ck = _fresh_cookies()
        if ck:
            cmd += ["--cookies", ck]
        cmd += ["--", "ytsearch10:" + query]
        try:
            rc, out, err = self._run_ydl(cmd, timeout=90)
        except subprocess.TimeoutExpired:
            return []
        finally:
            _drop_cookies(ck)
        if rc != 0:
            log("yt search err: %s" % (err or out or "")[-200:])
            return []
        items = []
        for ln in (out or "").splitlines():
            parts = ln.split("\t", 1)
            if len(parts) != 2:
                continue
            t, u = parts[0].strip(), parts[1].strip()
            if re.search(r"youtube\.com/watch\?v=|youtu\.be/", u):
                items.append({"key": u, "title": t or u})
            if len(items) >= 10:
                break
        return items

    def _canonical_yt_url(self, url):
        """YouTube IDs are case-sensitive: a lowercased pasted link fails with
        'Video unavailable'. Recover the canonical spelling by searching the id
        text and picking the result whose id matches case-insensitively."""
        m = re.search(r"(?:watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
        if not m:
            return url
        vid = m.group(1)
        if not re.search(r"[A-Za-z]", vid):
            return url
        try:
            items = self._yt_search_list(vid)
        except Exception:
            return url
        for it in items:
            f = re.search(r"(?:watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})", it["key"])
            if f and f.group(1).lower() == vid.lower():
                if f.group(1) != vid:
                    return url.replace(vid, f.group(1))
                break
        return url

    def _is_yt_playlist_url(self, url):
        low = url.lower()
        if "youtube.com" not in low and "youtu.be" not in low:
            return False
        # watch/shorts/embed links (even with &list= autoplay param) are single videos
        if "watch?" in low or "/shorts/" in low or "/embed/" in low:
            return False
        return bool(re.search(r"(^|[/?&])list=", url)) or "/playlist" in low

    def _yt_playlist_items(self, url, limit=PLAYLIST_LIMIT):
        """Return list of (video_url, title) for a YouTube playlist (no download)."""
        cmd = list(YTDLP) + [
            "--flat-playlist",
            "--playlist-items", "1-%d" % limit,
            "--no-warnings",
            "--print", "%(title)s\t%(url)s",
        ]
        if YT_PO_TOKEN:
            cmd += ["--extractor-args", YT_PO_TOKEN]
        ck = _fresh_cookies()
        if ck:
            cmd += ["--cookies", ck]
        cmd += ["--", url]
        try:
            rc, out, err = self._run_ydl(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            return []
        finally:
            _drop_cookies(ck)
        if rc != 0:
            log("yt playlist err: %s" % (err or out or "")[-200:])
            return []
        items = []
        for ln in (out or "").splitlines():
            parts = ln.split("\t", 1)
            if len(parts) != 2:
                continue
            t, u = parts[0].strip(), parts[1].strip()
            if re.search(r"youtube\.com/watch\?v=|youtu\.be/", u):
                items.append((u, t or u))
            if len(items) >= limit:
                break
        return items

    def _is_ym_playlist_url(self, url):
        return bool(re.search(r"music\.yandex\.[^/]+/(?:users/[^/]+/playlists/\d+|playlists/[^/]+)", url))

    def _ym_playlist_items(self, url, limit=PLAYLIST_LIMIT):
        """Return list of (ymtrack:<id>, label) for a Yandex Music playlist.

        Supports both link shapes:
          music.yandex.ru/users/<login>/playlists/<kind>
          music.yandex.ru/playlists/lk.<uuid>   (personal/shared playlist links)
        """
        if not YM_TOKEN:
            return []
        m = re.search(r"music\.yandex\.[^/]+/users/([^/]+)/playlists/(\d+)", url)
        m2 = re.search(r"music\.yandex\.[^/]+/playlists/([^/]+)", url)
        try:
            from yandex_music import Client
            client = Client(YM_TOKEN).init()
            if m:
                pl = client.users_playlists(kind=int(m.group(2)), user_id=m.group(1))
            elif m2:
                pl = client.playlist(m2.group(1))
            else:
                return []
            if not pl:
                return []
            items = []
            for ts in (pl.fetch_tracks() or []):
                tr = ts.track if ts else None
                if not tr or not tr.id:
                    continue
                label = " - ".join(a.name for a in (tr.artists or [])) + " - " + tr.title
                items.append(("ymtrack:%s" % tr.id, label))
                if len(items) >= limit:
                    break
            return items
        except Exception as e:
            log("ym playlist err: %s" % e)
            return []

    def _playlist_worker(self, url):
        try:
            if self._is_ym_playlist_url(url):
                items = self._ym_playlist_items(url)
            else:
                items = self._yt_playlist_items(url)
            self.api_q.put(("playlist_done", url, items))
        except Exception as e:
            log("playlist worker err: %s" % e)
            self.api_q.put(("playlist_done", url, []))

    def _normalize_ym_url(self, url):
        """Map a direct Yandex.Music track URL (album/<a>/track/<id> or
        track/<id>) to its ymtrack:<id> key. yt-dlp cannot handle such URLs
        and crashes with a TypeError, so they must go through the YM API."""
        m = re.search(r"music\.yandex\.[^/\s]+/(?:album/[^/?#]+/)?track/(\d+)", url)
        if m:
            return "ymtrack:%s" % m.group(1)
        return url

    def _handle_url(self, url, label):
        """Play a direct link; a YouTube/Yandex playlist queues all its tracks."""
        url = self._normalize_ym_url(url)
        if self._is_yt_playlist_url(url) or self._is_ym_playlist_url(url):
            self._send("📃 Плейлист: собираю треки…")
            threading.Thread(target=self._playlist_worker, args=(url,), daemon=True).start()
            return
        self._switch_to(url, label)

    def _download_worker(self, url, title):
        real_url = url
        if url.startswith("ytsearch1:"):
            q = url.split(":", 1)[1]
            if not self.silent:
                self.api_q.put(("status", "🔎 Ищу на YouTube: %s…" % q))
            items = self._yt_search_list(q)
            if not items:
                self.api_q.put(("download_fail", url, title, "Поиск не нашёл видео"))
                return
            real_url = items[0]["key"]
        ym_title = None
        if url.startswith("ymtrack:"):
            tid = url.split(":", 1)[1]
            if not self.silent:
                self.api_q.put(("status", "🎵 Ищу трек на Яндекс.Музыке…"))
            real_url, ym_title = self._ym_resolve(tid)
            if not real_url:
                self.api_q.put(("download_fail", url, title, ym_title or "не нашёл"))
                return
            title = ym_title or title
        elif url.startswith("ymsearch1:"):
            q = url.split(":", 1)[1]
            if not self.silent:
                self.api_q.put(("status", "🔎 Ищу на Яндекс.Музыке: %s…" % q))
            items = self._ym_search_list(q)
            if not items:
                self.api_q.put(("download_fail", url, title, "Ничего не нашёл на Яндекс.Музыке."))
                return
            real_url, ym_title = self._ym_resolve(items[0]["key"].split(":", 1)[1])
            if not real_url:
                self.api_q.put(("download_fail", url, title, ym_title or "не нашёл"))
                return
            title = ym_title or title
        canon_done = False
        alt_tried = False  # запасные клиенты YouTube пробуем один раз на цикл
        for attempt in range(1, 4):
            try:
                out = os.path.join(CACHE_DIR, uuid.uuid4().hex)
                if ym_title:
                    try:
                        mp3 = out + ".mp3"
                        req = urllib.request.Request(real_url, headers={"User-Agent": "yandex-music/3.0.0"})
                        with urllib.request.urlopen(req, timeout=180) as r, open(mp3, "wb") as f:
                            shutil.copyfileobj(r, f)
                        if not os.path.exists(mp3) or os.path.getsize(mp3) < 1024:
                            raise Exception("пустой файл")
                        self.api_q.put(("download_ok", url, ym_title, mp3))
                        return
                    except Exception as e:
                        self.api_q.put(("download_fail", url, title, "Не удалось скачать с Яндекс.Музыки: %s" % str(e)[:150]))
                        return
                cmd = list(YTDLP) + [
                    "--no-playlist",
                    "--no-simulate",
                    # Без mp3-транскода: он на этом сервере жрёт ~16с на трек.
                    # Качаем лучший аудио-поток как есть (обычно opus/webm или m4a)
                    # и играем его через ffmpeg — в голосовом канале контейнер не важен.
                    "-f", "ba/b",
                    "--remote-components", "ejs:github",
                    "-o", out + ".%(ext)s",
                    "--print", "%(title)s",
                ]
                if YT_JS_RUNTIME:
                    cmd += ["--js-runtimes", "deno:%s" % YT_JS_RUNTIME]
                if YT_PO_TOKEN:
                    cmd += ["--extractor-args", YT_PO_TOKEN]
                ck = _fresh_cookies(RUTUBE_COOKIES if "rutube.ru" in real_url else COOKIES)
                if ck:
                    cmd += ["--cookies", ck]
                cmd += ["--", real_url]
                try:
                    rc, stdout, stderr = self._run_ydl(cmd, timeout=600)
                finally:
                    _drop_cookies(ck)
                audio = _find_downloaded(out)
                if rc != 0 or not audio:
                    err_text = (stderr or stdout or "yt-dlp failed").strip()
                    is_yt = "youtube.com" in real_url or "youtu.be" in real_url
                    if ("Video unavailable" in err_text and not canon_done
                            and ("youtube.com/watch?v=" in real_url or "youtu.be/" in real_url)):
                        new_url = self._canonical_yt_url(real_url)
                        if new_url != real_url:
                            log("yt id case fix: %s -> %s" % (real_url, new_url))
                            real_url = new_url
                            canon_done = True
                            continue
                    # основной клиент не взял — пробуем запасные клиенты YouTube
                    if is_yt and not alt_tried:
                        alt_tried = True
                        arc, aout, aerr = self._yt_alt_download(out, real_url)
                        if arc == 0:
                            rc, stdout, stderr = 0, aout, aerr
                            audio = _find_downloaded(out)
                        elif aerr:
                            err_text = aerr
                    # rc=0 без файла (молчаливый отказ YouTube) — это тоже ошибка
                    if rc != 0 or not audio:
                        if "Sign in to confirm" in err_text or "LOGIN_REQUIRED" in err_text:
                            self.api_q.put(("download_fail", url, title, "YouTube это видео с сервера не отдаёт — просит войти в аккаунт (обычно возрастное ограничение). С серверного адреса такое не обойти, попробуй другой ролик."))
                            return
                        err_msg = (err_text.splitlines()[-1] if err_text else "yt-dlp failed")[:300]
                        if attempt < 3:
                            self.api_q.put(("status", "⚠ %s. Повтор %d/3 через 15с…" % (err_msg, attempt)))
                            time.sleep(15)
                            continue
                        self.api_q.put(("download_fail", url, title, err_msg))
                        return
                got_title = (stdout or "").strip().splitlines()
                real_title = got_title[0] if got_title else title
                self.api_q.put(("download_ok", url, real_title, audio))
                return
            except subprocess.TimeoutExpired:
                if attempt < 3:
                    self.api_q.put(("status", "⚠ Таймаут скачивания. Повтор %d/3 через 15с…" % attempt))
                    time.sleep(15)
                    continue
                self.api_q.put(("download_fail", url, title, "timeout"))
                return
            except Exception as e:
                if attempt < 3:
                    self.api_q.put(("status", "⚠ Ошибка: %s. Повтор %d/3 через 15с…" % (str(e)[:120], attempt)))
                    time.sleep(15)
                    continue
                self.api_q.put(("download_fail", url, title, str(e)[:300]))
                return

    def _set_status(self, text):
        """Set the bot's TeamTalk status line (visible in the client)."""
        try:
            self.doChangeStatus(0, text)
        except Exception as e:
            log("status err: %s" % e)

    def _start_voice(self, path, offset_ms=0):
        self._stop_voice()
        self.voice_stop.clear()
        self.voice_offset_base = int(offset_ms)
        self.voice_started_at = time.time()
        t = threading.Thread(
            target=self._voice_worker, args=(path, int(offset_ms)), daemon=True
        )
        self.voice_thread = t
        t.start()

    def _voice_worker(self, path, offset_ms):
        """Play audio to TeamTalk as voice.

        With PULSE_SINK set, ffmpeg plays the track into that PulseAudio sink
        (real-time via -re) and parec captures the sink's monitor back, so the
        bot relays whatever is audible on the machine. Otherwise ffmpeg decodes
        the file straight to raw PCM — no sound devices involved.
        """
        pulse = PULSE_SINK
        cap = None  # parec process (pulse mode only)
        try:
            if pulse:
                cmd = ["ffmpeg", "-y"]
                if offset_ms > 0:
                    cmd += ["-ss", "%.3f" % (offset_ms / 1000.0)]
                cmd += ["-re", "-i", path, "-vn", "-f", "pulse", pulse]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                cap = subprocess.Popen(
                    ["parec", "--device=%s.monitor" % pulse, "--format=s16le",
                     "--rate", str(VOICE_RATE), "--channels=%d" % VOICE_CHANNELS],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                src = cap.stdout
            else:
                cmd = ["ffmpeg", "-y"]
                if offset_ms > 0:
                    cmd += ["-ss", "%.3f" % (offset_ms / 1000.0)]
                cmd += ["-i", path, "-vn", "-f", "s16le", "-ac", str(VOICE_CHANNELS),
                        "-ar", str(VOICE_RATE), "-"]
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                src = proc.stdout
        except Exception as e:
            self.api_q.put(("voice_error", "ffmpeg: %s" % str(e)[:120]))
            return
        self.voice_proc = proc
        stream_id = int(time.time() * 1000) & 0xFFFF
        finished = False
        buf = b""
        block_dur = VOICE_CHUNK / float(VOICE_RATE)  # 0.02 s per 20 ms block
        next_slot = time.monotonic()
        drain_until = None  # pulse mode: tail-drain deadline after player exits
        try:
            while not self.voice_stop.is_set():
                if pulse and proc.poll() is not None and drain_until is None:
                    drain_until = time.monotonic() + 0.4
                if len(buf) < VOICE_CHUNK_BYTES:
                    r, _, _ = select.select([src], [], [], 0.1)
                    if r:
                        data = src.read(VOICE_CHUNK_BYTES)
                        if not data:
                            finished = True
                            break
                        buf += data
                if pulse and drain_until is not None and time.monotonic() >= drain_until:
                    finished = True
                    break
                if len(buf) < VOICE_CHUNK_BYTES:
                    continue  # partial/empty read: wait for a full block
                # Feed blocks on a strict 20 ms schedule. A fixed sleep after
                # each insert drifts (read + insert take time) and stutters;
                # sleeping to the exact next slot keeps the stream smooth.
                next_slot += block_dur
                delay = next_slot - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_slot = time.monotonic()  # behind: don't compound backlog
                chunk = buf[:VOICE_CHUNK_BYTES]
                buf = buf[VOICE_CHUNK_BYTES:]
                chunk = self._scale_pcm(chunk)
                raw = (ctypes.c_char * VOICE_CHUNK_BYTES).from_buffer_copy(chunk)
                ab = TeamTalk5.AudioBlock()
                ab.nStreamID = stream_id
                ab.nSampleRate = VOICE_RATE
                ab.nChannels = VOICE_CHANNELS
                ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
                ab.nSamples = VOICE_CHUNK
                ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
                try:
                    self.insertAudioBlock(ab)
                except Exception as e:
                    log("insertAudioBlock err: %s" % e)
        except Exception as e:
            log("voice worker err: %s" % e)
        finally:
            if buf:
                pad = VOICE_CHUNK_BYTES - (len(buf) % VOICE_CHUNK_BYTES)
                if pad and pad != VOICE_CHUNK_BYTES:
                    buf += b"\x00" * pad
                for i in range(0, len(buf), VOICE_CHUNK_BYTES):
                    chunk = buf[i:i + VOICE_CHUNK_BYTES]
                    if len(chunk) < VOICE_CHUNK_BYTES:
                        chunk += b"\x00" * (VOICE_CHUNK_BYTES - len(chunk))
                    chunk = self._scale_pcm(chunk)
                    raw = (ctypes.c_char * VOICE_CHUNK_BYTES).from_buffer_copy(chunk)
                    ab = TeamTalk5.AudioBlock()
                    ab.nStreamID = stream_id
                    ab.nSampleRate = VOICE_RATE
                    ab.nChannels = VOICE_CHANNELS
                    ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
                    ab.nSamples = VOICE_CHUNK
                    ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
                    try:
                        self.insertAudioBlock(ab)
                    except Exception as e:
                        log("insertAudioBlock err: %s" % e)
            try:
                self.insertAudioBlockEnd()
            except Exception:
                pass
            for p in (proc, cap):
                try:
                    if p is not None and p.poll() is None:
                        p.kill()
                except Exception:
                    pass
            self.voice_proc = None
            if finished:
                self.api_q.put(("voice_finished", path))

    def _stop_voice(self):
        self.voice_stop.set()
        t = self.voice_thread
        self.voice_thread = None
        if t and t.is_alive():
            t.join(timeout=1.5)
        try:
            self.insertAudioBlockEnd()
        except Exception:
            pass

    def _tts_announce(self, text):
        """Синтезировать озвучку текста (Google Translate TTS) в файл. Вернуть путь или None."""
        try:
            url = ("https://translate.google.com/translate_tts?ie=UTF-8&tl=ru&client=tw-ob&q="
                   + urllib.parse.quote(text))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            if not data:
                return None
            path = os.path.join(CACHE_DIR, "announce_%d.mp3" % int(time.time() * 1000))
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception as e:
            log("tts announce err: %s" % str(e)[:120])
            return None

    def _play_file(self, url, title, path):
        self.current_orig = path
        self.current_file = path
        self.current = (url, title)
        self.cur_source = (url, title, False)
        self.playing = True
        self.paused = False
        self.cur_offset_ms = 0
        if not self.silent:
            self._send("▶ Сейчас играет: %s" % title)
        self._set_status("Playing: %s" % title)
        if self.voice_announce and title:
            ann = self._tts_announce("Сейчас играет: %s" % title)
            if ann:
                self._announce_pending = (path, 0)
                self._start_voice(ann, 0)
                return
        self._start_voice(path, 0)
        self._music_broadcast(path, title)  # раздать трек подписчикам музыки (в фоне)

    def _play_local(self, path, title):
        """Play a local file (no download) — used for files sent via Telegram."""
        self.auto_list = False
        self.auto_playlist = False
        self.queue.clear()
        self.downloading.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = None
        self.cur_offset_ms = 0
        self._play_file(path, title, path)

    def _elapsed_ms(self):
        if self.playing and not self.paused and self.voice_started_at:
            return self.voice_offset_base + int(
                (time.time() - self.voice_started_at) * 1000
            )
        return self.cur_offset_ms

    def _pause(self):
        if not self.playing or self.paused:
            return
        self.cur_offset_ms = self._elapsed_ms()
        self.paused = True
        self._stop_voice()
        self._set_status("Paused: %s" % (self.current[1] if self.current else ""))
        self._send("⏸ Пауза (%s)" % _fmt_ms(self.cur_offset_ms))

    def _resume(self):
        if not self.playing or not self.paused or not self.current_orig:
            return
        self.paused = False
        self._set_status("Playing: %s" % (self.current[1] if self.current else ""))
        self._start_voice(self.current_orig, self.cur_offset_ms)
        self._send("▶ Продолжаю (%s)" % _fmt_ms(self.cur_offset_ms))

    def _set_volume(self, v):
        v = max(1, min(MAX_VOLUME, v))
        self.volume = v
        # No ffmpeg restart needed: _scale_pcm applies the new gain to the next
        # audio block, so volume changes take effect instantly for any source.
        self._send("🔊 Громкость: %d%%" % v)

    def _seek(self, delta_s):
        """Seek the current track by delta_s seconds (negative = backwards)."""
        if not self.playing or not self.current_orig:
            self._send("Сейчас ничего не играет.")
            return
        cur = self.cur_offset_ms if self.paused else self._elapsed_ms()
        new_ms = max(0, cur + delta_s * 1000)
        self.cur_offset_ms = new_ms
        arrow = "⏪" if delta_s < 0 else "⏩"
        if self.paused:
            self._set_status("Paused: %s" % (self.current[1] if self.current else ""))
            self._send("%s %s → ⏱ %s" % (arrow, ("%dс" % delta_s), _fmt_ms(new_ms)))
        else:
            self._set_status("Playing: %s" % (self.current[1] if self.current else ""))
            self._start_voice(self.current_orig, new_ms)
            self._send("%s %s → ⏱ %s" % (arrow, ("%dс" % delta_s), _fmt_ms(new_ms)))

    def _switch_to(self, key, label):
        """Stop whatever plays and immediately play `key` (used by n/b and direct links)."""
        key = self._normalize_ym_url(key)
        self.auto_list = False
        self.auto_playlist = False
        self.queue.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = None
        self.cur_offset_ms = 0
        self._set_status(self.status_msg)
        self._enqueue_url(key, label)

    def _play_search_index(self, idx, silent=False):
        self.silent = silent
        if not self.search_results:
            self._send("Список результатов пуст. Сначала поищи: /play <запрос>")
            return
        if idx < 0 or idx >= len(self.search_results):
            self._send("Нет результата под номером %d (всего %d)." % (idx + 1, len(self.search_results)))
            return
        self.search_index = idx
        item = self.search_results[idx]
        self._switch_to(item["key"], "🎵 %d. %s" % (idx + 1, item["title"]))
        self.auto_list = True

    def _play_playlist_index(self, idx, silent=False):
        """Play track `idx` of the current playlist (YouTube / Yandex Music)."""
        self.silent = silent
        if not self.playlist:
            self._send("Список плейлиста пуст. Вставь ссылку на плейлист.")
            return
        if idx < 0 or idx >= len(self.playlist):
            self._send("Нет трека под номером %d (всего %d)." % (idx + 1, len(self.playlist)))
            return
        self.playlist_index = idx
        u, t = self.playlist[idx]
        self._switch_to(u, "🎵 %d. %s" % (idx + 1, t))
        self.auto_playlist = True

    def _playlist_page_lines(self, page=1, per_page=20):
        """Numbered page of the loaded playlist (whole list fits via pages)."""
        if not self.playlist:
            return ["Список плейлиста пуст. Вставь ссылку на плейлист."]
        total = len(self.playlist)
        pages = max(1, (total + per_page - 1) // per_page)
        page = max(1, min(pages, page))
        start = (page - 1) * per_page
        end = min(total, start + per_page)
        lines = ["📃 Плейлист: %d треков (стр. %d/%d)" % (total, page, pages)]
        for i in range(start, end):
            _u, t = self.playlist[i]
            lines.append("%d. %s" % (i + 1, t[:45]))
        lines.append("playlist <страница> — листать, n/b — следующий/предыдущий трек")
        return lines

    def _do_search(self, query):
        self._send("🔎 Ищу: %s…" % query)
        threading.Thread(target=self._search_worker, args=(query,), daemon=True).start()

    def _search_worker(self, query):
        try:
            if self.service == "ym":
                items = self._ym_search_list(query)
            else:
                items = self._yt_search_list(query)
            self.api_q.put(("search_done", query, items))
        except Exception as e:
            log("search err: %s" % e)
            self.api_q.put(("search_done", query, []))

    def _advance(self, silent=False):
        self.silent = silent
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = None
        self.cur_offset_ms = 0
        self.voice_offset_base = 0
        self.voice_started_at = 0
        self._set_status(self.status_msg)
        if self.auto_playlist and self.playlist and self.playlist_index + 1 < len(self.playlist):
            # auto-advance through the playlist
            self._play_playlist_index(self.playlist_index + 1, silent=True)
            return
        if self.queue:
            self.queue.pop(0)
        if self.queue:
            self._enqueue_next()
        elif self.auto_list and self.search_results and self.search_index + 1 < len(self.search_results):
            # auto-advance: keep playing the rest of the search-result list
            self.search_index += 1
            self._play_search_index(self.search_index, silent=True)
        else:
            ended = self.auto_list or self.auto_playlist
            self.auto_list = False
            self.auto_playlist = False
            self._send("⏹ Конец списка." if ended else "⏹ Очередь пуста.")

    # ----- favorites (избранное) -----
    def _load_favorites(self):
        """Load favorites from favorites.json; survives restarts."""
        try:
            with open(FAVORITES_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return [i for i in data.get("items", []) if i.get("key")]
        except Exception:
            return []

    def _save_favorites(self):
        try:
            with open(FAVORITES_FILE, "w", encoding="utf-8") as f:
                json.dump({"items": self.favorites}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("fav save err: %s" % e)

    def _fav_cmd(self, arg):
        """Favorites: f — list, f + — add current track, f + <url> — add a link, f <n> — play."""
        if not arg:
            if not self.favorites:
                self._send("Избранное пусто. Добавь: f + (текущий трек) или f + <ссылка>.")
                return
            lines = ["Избранное:"]
            for i, it in enumerate(self.favorites, 1):
                lines.append("%d. %s" % (i, it["label"]))
            lines.append("f <номер> — играть, f + <ссылка> — добавить, f - <номер> — удалить")
            self._send("\n".join(lines))
            return
        if arg == "+":
            if not self.cur_source:
                self._send("Сейчас ничего не играет. Добавь ссылку: f + <ссылка>.")
                return
            key, label, is_radio = self.cur_source
            if any(it["key"] == key for it in self.favorites):
                self._send("Уже в избранном: %s" % label)
                return
            self.favorites.append({"key": key, "label": label, "radio": is_radio})
            self._save_favorites()
            self._send("⭐ Добавлено в избранное: %s" % label)
            return
        if arg.startswith("+ "):
            u = URL_RE.search(arg[2:])
            if not u:
                self._send("Дай ссылку: f + https://…")
                return
            key = self._normalize_ym_url(u.group(0))
            if any(it["key"] == key for it in self.favorites):
                self._send("Уже в избранном: %s" % key)
                return
            self.favorites.append({"key": key, "label": key, "radio": False})
            self._save_favorites()
            self._send("⭐ Добавлено в избранное: %s" % key)
            return
        if arg == "-" or arg.startswith("- "):
            num = arg[1:].strip()
            if not num.isdigit():
                self._send("Формат удаления: f - <номер>, напр. f - 1.")
                return
            idx = int(num) - 1
            if idx < 0 or idx >= len(self.favorites):
                self._send("Нет записи под номером %d (всего %d)." % (idx + 1, len(self.favorites)))
                return
            it = self.favorites.pop(idx)
            self._save_favorites()
            self._send("🗑 Удалено из избранного: %s" % it["label"])
            return
        if arg.isdigit():
            idx = int(arg) - 1
            if idx < 0 or idx >= len(self.favorites):
                self._send("Нет записи под номером %d (всего %d)." % (idx + 1, len(self.favorites)))
                return
            it = self.favorites[idx]
            if it.get("radio"):
                self._play_radio(it["key"], it["label"])
            else:
                self._switch_to(it["key"], it["label"])
            return
        self._send("Команды: f — список, f + — добавить текущий, f + <ссылка> — добавить, f <номер> — играть, f - <номер> — удалить")

    # ----- radio stations (m3u playlists) -----
    def _load_radio(self):
        """Scan BASE_DIR/radio for .m3u files, flat list of stations."""
        stations = []
        base = os.path.join(BASE_DIR, "radio")
        if not os.path.isdir(base):
            return stations
        for fn in sorted(os.listdir(base)):
            if not fn.lower().endswith(".m3u"):
                continue
            full = os.path.join(base, fn)
            title, url = self._parse_m3u(full, fn)
            if url:
                stations.append((title, url))
        return stations

    def _parse_m3u(self, path, fallback_name):
        """Return (title, stream_url) from an m3u file, or (fallback, None)."""
        title = os.path.splitext(fallback_name)[0]
        url = None
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    if ln.upper().startswith("#EXTINF"):
                        m = re.search(r",\s*(.+)\s*$", ln)
                        if m and m.group(1).strip():
                            title = m.group(1).strip()
                    elif ln.startswith("http://") or ln.startswith("https://"):
                        url = ln
                        break
        except Exception as e:
            log("m3u parse err %s: %s" % (path, e))
        return title, url

    def _radio_cmd(self, arg):
        """Radio station browser: плоский список → запуск по номеру."""
        if not self.radio:
            self._send("Нет папки radio/ с m3u. Положи станции и перезапусти.")
            return
        if not arg:
            lines = ["📻 Радио (%d станций):" % len(self.radio)]
            for i, (t, _u) in enumerate(self.radio, 1):
                lines.append("%d. %s" % (i, t))
                if i >= 15:
                    lines.append("… и ещё %d" % (len(self.radio) - 15))
                    break
            lines.append("radio <номер> — запуск")
            self._send("\n".join(lines))
            return
        if arg.isdigit():
            idx = int(arg) - 1
            if idx < 0 or idx >= len(self.radio):
                self._send("Нет станции под номером %d (всего %d)." % (idx + 1, len(self.radio)))
                return
            title, url = self.radio[idx]
            self._play_radio(url, title)
            return
        # текстовый поиск по названию станции
        q = arg.lower()
        found = [(i + 1, t) for i, (t, _u) in enumerate(self.radio) if q in t.lower()]
        if not found:
            self._send("Не нашёл станцию «%s»." % arg)
            return
        lines = ["Нашёл:"]
        for idx, t in found[:10]:
            lines.append("%d. %s" % (idx, t))
        self._send("\n".join(lines) + "\nradio <номер> — запуск")

    def _play_radio(self, url, title):
        """Play an internet radio stream directly (no download)."""
        self.auto_list = False
        self.auto_playlist = False
        self.queue.clear()
        self.downloading.clear()
        self._stop_voice()
        self.playing = False
        self.paused = False
        self.current = None
        self.current_orig = None
        self.cur_source = (url, title, True)
        self.cur_offset_ms = 0
        self._send("📻 ▶ %s" % title)
        self._set_status("Radio: %s" % title)
        self._start_voice(url, 0)

    # Команды-«запросы», которые при приходе из Telegram объявляются в канале TeamTalk.
    _TG_KNOWN_CMDS = frozenset([
        "v", "volume", "sf", "sb", "sub", "cm", "vo", "rs", "restart", "sv", "svc",
        "cn", "cs", "s", "stop", "skip", "queue", "q", "status", "now",
        "dl", "download", "lf", "n", "next", "b", "back", "prev", "playlist",
        "radio", "r", "f", "fav", "favorites", "play", "p", "pause", "resume",
        "channel", "u", "link", "url",
    ])

    def _is_known_cmd(self, text):
        """Первый токен (со слешем или без) — известная команда бота."""
        t = (text or "").strip()
        if not t:
            return False
        if t.startswith("/"):
            t = t[1:]
        return t.split(None, 1)[0].lower() in self._TG_KNOWN_CMDS

    def _tg_cmd_author(self, msg):
        """Имя автора команды из Telegram: first_name + last_name, иначе username."""
        frm = msg.get("from") or {}
        name = " ".join(x for x in (frm.get("first_name") or "", frm.get("last_name") or "") if x).strip()
        return name or frm.get("username") or "Telegram"

    def _tg_cmd_female(self, msg):
        """True, если автор команды — подписчик, чей пол в TeamTalk женский."""
        try:
            cid = (msg.get("from") or {}).get("id")
            for sid, info in self.sub_active.items():
                if str(sid) == str(cid):
                    uname = (info.get("username") or "").lower()
                    for u in self.users.values():
                        if (self._tt_field(u, "szUsername") or "").lower() == uname:
                            return self._user_female(u)
        except Exception:
            pass
        return False

    def _send_channel_announce(self, text):
        """Служебное сообщение в текущий канал TeamTalk (без зеркала в Telegram)."""
        try:
            if self.my_channel_id:
                msgs = buildTextMessage(text, TextMsgType.MSGTYPE_CHANNEL, nChannelID=self.my_channel_id)
                for m in msgs:
                    self.doTextMessage(m)
                log("channel announce: %s" % text[:100])
        except Exception as e:
            log("channel announce err: %s" % str(e)[:150])

    def _announce_tg_cmd(self, msg, text):
        """Команда из Telegram объявляется в канале: «Кирилл запросил: радио ремикс FM»."""
        try:
            if not self._is_known_cmd(text):
                return
            author = self._tg_cmd_author(msg)
            verb = "запросила" if self._tg_cmd_female(msg) else "запросил"
            body = (text or "").strip()
            if body.startswith("/"):
                body = body[1:]
            self._send_channel_announce("%s %s: %s" % (author, verb, body))
        except Exception as e:
            log("announce tg cmd err: %s" % str(e)[:150])

    def _handle_cmd(self, text, from_user):
        text = text.strip()
        low = text.lower()
        cmd = low[1:] if low.startswith("/") else low
        if from_user:
            self.reply_user_id = from_user
        self.silent = False  # explicit command → report status again

        # --- гейт: служебные команды — только администраторам ---
        # (в TeamTalk админ = USERTYPE_ADMIN; из Telegram сюда попадают уже после _tg_allowed)
        first = cmd.split(None, 1)[0]
        admin_only = first in ("rs", "restart", "cn", "cs", "sv", "svc", "cm", "channel", "sc", "save", "login")
        if not admin_only:
            admin_only = cmd.startswith("lf ")
        if admin_only and not self._is_admin(from_user):
            self._send("Эта команда только для администраторов.")
            return

        # --- громкость: v 100 / v 50 / громкость 30 / volume 80 ---
        m = re.match(r"^(?:v|volume)\s+(\d{1,3})$", cmd)
        if m:
            self._set_volume(int(m.group(1)))
            return

        # --- перемотка: sf 5 (вперёд на 5с), sf -5 (назад на 5с) ---
        m = re.match(r"^sf\s+(-?\d{1,6})$", cmd)
        if m:
            self._seek(int(m.group(1)))
            return
        # --- перемотка назад: sb 66 (на 66с назад) ---
        m = re.match(r"^sb\s+(\d{1,6})$", cmd)
        if m:
            self._seek(-int(m.group(1)))
            return

        # --- подписка на уведомления: sub / /sub; подписка на музыку: sub mus ---
        if cmd == "sub" or cmd.startswith("sub "):
            rest = text[len("sub"):].strip().lower()
            if rest in ("mus", "music"):
                self._sub_music_cmd()
            else:
                self._sub_cmd()
            return

        # --- сообщения в канал: cm — вкл/выкл (по умолчанию ответы в личку) ---
        if cmd == "cm":
            self.channel_msg = not self.channel_msg
            self._save_channel_msg()
            self._send("Сообщения в канал: %s" % ("вкл ✅" if self.channel_msg else "выкл ⭕"))
            return

        # --- озвучка названия трека: vo — вкл/выкл ---
        if cmd == "vo":
            self.voice_announce = not self.voice_announce
            self._save_voice_announce()
            self._send("Озвучка названий: %s" % ("вкл 🔊" if self.voice_announce else "выкл ⭕"))
            return

        # --- вход в YouTube своим аккаунтом: login (статус + как войти) ---
        if cmd == "login":
            self._cmd_login()
            return

        # --- перезапуск бота: rs ---
        if cmd in ("rs", "restart"):
            self._send("🔄 Перезапускаюсь…")
            threading.Thread(target=_restart_bot_soon, daemon=True).start()
            return

        # --- выбор сервиса: sv yt / sv ym / sv ---
        first = cmd.split(None, 1)[0]
        if first in ("sv", "svc"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                cur = {"yt": "YouTube", "ym": "Яндекс.Музыка"}.get(self.service, "?")
                self._send("Сейчас: %s. Сменить: sv yt или sv ym." % cur)
                return
            svc = parts[1].strip().lower()
            if svc in ("yt", "youtube"):
                self.service = "yt"
                self._send("🎬 Сервис: YouTube.")
            elif svc in ("ym", "ya", "yandex"):
                self.service = "ym"
                self._send("🎵 Сервис: Яндекс.Музыка.")
            else:
                self._send("Не знаю сервис «%s». Доступно: yt (YouTube), ym (Яндекс.Музыка)." % svc)
            return

        # --- сохранить настройки сессии в config.json: sc ---
        if cmd in ("sc", "save"):
            svc = {"yt": "YouTube", "ym": "Яндекс.Музыка"}.get(self.service, self.service)
            try:
                _save_config({
                    "server": {"nickname": self.nickname},
                    "runtime": {"main_service": self.service},
                    "playback": {"default_volume": int(self.volume)},
                })
                self._send("💾 Сохранено в config.json: ник «%s», сервис %s, громкость %d."
                           % (self.nickname, svc, self.volume))
            except Exception as e:
                log("sc save err: %s" % e)
                self._send("Не удалось сохранить в config.json: %s" % e)
            return

        # --- смена ника: cn <ник> (в текущей сессии; навсегда — /sc) ---
        if cmd == "cn" or cmd.startswith("cn "):
            parts = text.split(None, 1)
            if len(parts) < 2 or not parts[1].strip():
                self._send("Использование: cn <ник>, напр. cn музыкант")
                return
            nick = parts[1].strip()
            if len(nick) > 255:
                self._send("Ник слишком длинный (максимум 255 символов).")
                return
            self.nickname = nick
            self.doChangeNickname(nick)
            self._send("✅ Ник: %s. Чтобы сохранить навсегда — /sc" % nick)
            return

        # --- смена статусного сообщения: cs <текст> (пусто = очистить) ---
        if cmd == "cs" or cmd.startswith("cs "):
            parts = text.split(None, 1)
            status = parts[1].strip() if len(parts) > 1 else ""
            self.status_msg = status
            try:
                with open(STATUS_MSG_FILE, "w") as f:
                    f.write(status)
            except Exception as e:
                log("status msg save err: %s" % e)
            self._set_status(status)
            self._send("✅ Статус: %s" % (status or "(пусто — очищен)"))
            return

        # --- стоп / скип / очередь / статус / помощь ---
        if cmd in ("s", "stop"):
            self.queue.clear()
            self.downloading.clear()
            self.auto_list = False
            self.auto_playlist = False
            self.silent = False
            self._stop_voice()
            self.playing = False
            self.paused = False
            self.current = None
            self.current_orig = None
            self.cur_offset_ms = 0
            self._announce_pending = None
            self._set_status(self.status_msg)
            self._send("⏹ Стоп.")
            return

        if cmd in ("skip",):
            self._advance()
            return

        if cmd in ("queue", "q"):
            self._queue_cmd()
            return

        if cmd in ("status", "now"):
            self._status_cmd()
            return

        # --- загрузить играющий трек файлом в канал (сервер TeamTalk): dl / скачать / download ---
        if cmd in ("dl", "download"):
            if not self.playing or not self.current_file:
                self._send("Сейчас ничего не играет — скачивать нечего.")
                return
            if self._dl_cmd_id is not None:
                self._send("Файл уже загружается — подожди.")
                return
            path = self.current_file
            if not os.path.isfile(path):
                self._send("Файл трека недоступен (это, вероятно, радио).")
                return
            title = (self.current or ("", "трек"))[1] or "трек"
            ext = os.path.splitext(path)[1] or ".mp3"
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", title).strip("_")[:80] or "track"
            # TT_DoSendFile кладёт файл на сервер под именем basename локального файла —
            # делаем копию с нормальным именем, чтобы в канале был читаемый файл
            tmp = os.path.join(CACHE_DIR, "upload_%d_%s%s" % (int(time.time() * 1000), safe, ext))
            try:
                shutil.copy2(path, tmp)
            except Exception as e:
                log("dl copy err: %s" % str(e)[:150])
                self._send("Ошибка: не могу подготовить файл (%s)" % str(e)[:100])
                return
            # Треки теперь хранятся как есть (opus/webm/m4a) без перекодирования;
            # в канал удобнее mp3 — конвертируем копию один раз, только по /dl.
            if ext.lower() != ".mp3":
                self._send("Готовлю mp3…")
                mp3tmp = os.path.join(CACHE_DIR, "upload_%d_%s.mp3" % (int(time.time() * 1000), safe))
                try:
                    rcc = subprocess.call(
                        ["ffmpeg", "-y", "-i", tmp, "-vn",
                         "-codec:a", "libmp3lame", "-q:a", "5", mp3tmp],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                except Exception:
                    rcc = 1
                if rcc == 0 and os.path.isfile(mp3tmp) and os.path.getsize(mp3tmp) > 1024:
                    try:
                        os.remove(tmp)
                    except Exception:
                        pass
                    tmp = mp3tmp
                else:
                    try:
                        os.remove(mp3tmp)
                    except Exception:
                        pass
            cid = self.my_channel_id or 1
            cmd_id = TeamTalk5._DoSendFile(self._tt, cid, _b(tmp))
            if cmd_id < 0:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
                self._send("⚠ Не удалось начать загрузку в канал.")
                return
            self._dl_cmd_id = cmd_id
            self._dl_remote = os.path.basename(tmp)
            self._dl_local = tmp
            self._send("📤 Загружаю «%s» в канал…" % self._dl_remote)
            return

        if cmd in ("help", "h"):
            self._help_cmd()
            return

        # --- локальный файл: lf <путь> (из Telegram-моста) ---
        if cmd.startswith("lf "):
            path = text.split(None, 1)[1].strip()
            if not os.path.isfile(path):
                self._send("Файл не найден: %s" % path)
                return
            self._play_local(path, os.path.basename(path))
            return

        # --- n/b: следующий/предыдущий (по активному плейлисту или списку поиска) ---
        if cmd in ("n", "next"):
            if self.auto_playlist:
                self._play_playlist_index(self.playlist_index + 1)
            elif self.auto_list:
                self._play_search_index(self.search_index + 1)
            elif self.playlist:
                self._play_playlist_index(self.playlist_index + 1)
            else:
                self._play_search_index(self.search_index + 1)
            return

        if cmd in ("b", "back", "prev"):
            if self.auto_playlist:
                self._play_playlist_index(self.playlist_index - 1)
            elif self.auto_list:
                self._play_search_index(self.search_index - 1)
            elif self.playlist:
                self._play_playlist_index(self.playlist_index - 1)
            else:
                self._play_search_index(self.search_index - 1)
            return

        # --- плейлист: пл / список / плейлист [<страница>] — полный список ---
        cmd_first = low.split(None, 1)[0]
        if cmd_first in ("playlist",):
            parts = text.split(None, 1)
            page = 1
            if len(parts) > 1 and parts[1].strip().isdigit():
                page = int(parts[1].strip())
            self._send("\n".join(self._playlist_page_lines(page)))
            return

        # --- радио: радио / радио <N> / радио <текст> ---
        if cmd.startswith("radio") or cmd == "r":
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            self._radio_cmd(arg)
            return

        # --- избранное: f / f + / f + <ссылка> / f <номер> ---
        if cmd in ("f", "fav", "favorites") or cmd.startswith(("f ", "fav ", "favorites ")):
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            self._fav_cmd(arg)
            return

        # --- play: с запросом/ссылкой — поиск и игра; голый — play/pause toggle ---
        if cmd == "play" or cmd.startswith("play ") or cmd == "p" or cmd.startswith("p "):
            parts = text.split(None, 1)
            arg = parts[1].strip() if len(parts) > 1 else ""
            if arg:
                u = URL_RE.search(arg)
                if u:
                    self._handle_url(u.group(0), u.group(0))
                else:
                    self._do_search(arg)
                return
            if self.paused:
                self._resume()
            elif self.playing:
                self._pause()
            else:
                self._send("Что играем? /play <запрос> или /play <ссылка>.")
            return

        # --- pause / resume: явные команды ---
        if cmd in ("pause", "resume"):
            if cmd == "pause" and self.playing and not self.paused:
                self._pause()
            elif cmd == "resume" and self.paused:
                self._resume()
            return

        # --- смена канала ---
        if cmd.startswith("channel"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self._send("Использование: /channel <путь>, напр. /channel /root/music")
                return
            path = parts[1].strip()
            cid = self.getChannelIDFromPath(path)
            if not cid:
                self._send("Канал не найден: %s" % path)
                return
            self.doJoinChannelByID(cid, CHANNEL_PASSWORD)
            self.play_channel_id = cid
            self._send("Перехожу в канал: %s" % path)
            return

        # --- прямая ссылка: u <url> / link <url> / url <url> ---
        # (cmd уже без слэша: "/u https://…" → cmd = "u https://…")
        first = cmd.split(None, 1)[0]
        if first in ("u", "link", "url"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                self._send("Использование: /u <ссылка>. Или просто вставь ссылку — сыграю сам.")
                return
            u = URL_RE.search(parts[1])
            if not u:
                self._send("Это не ссылка: «%s». Дай ссылку: /u https://…" % parts[1][:80])
                return
            self._handle_url(u.group(0), u.group(0))
            return

        # --- bare ссылка ---
        m = URL_RE.search(text)
        if m and not low.startswith("/"):
            self._switch_to(m.group(0), m.group(0))
            return

    def _enqueue_url(self, url, label):
        if url in [u for u, _ in self.queue]:
            if not self.silent:
                self._send("Уже в очереди: %s" % label)
            return
        self.queue.append((url, label))
        if not self.silent:
            self._send("➕ %s" % label)
        self._enqueue_next()

    def _queue_cmd(self):
        if not self.queue:
            self._send("Очередь пуста.")
        else:
            lines = ["Очередь:"]
            for i, (u, t) in enumerate(self.queue[:10], 1):
                lines.append("%d. %s (%s)" % (i, t, u))
            self._send("\n".join(lines))

    def _status_cmd(self):
        if self.playing and self.current:
            lines = ["▶ Играет: %s" % self.current[1]]
            lines.append("⏱ %s" % _fmt_ms(self._elapsed_ms()))
            if self.paused:
                lines.append("⏸ Пауза")
            if self.volume < 100:
                lines.append("🔊 %d%%" % self.volume)
        else:
            lines = ["Ничего не играет."]
        lines.append("Отправь help — справка по командам.")
        self._send("\n".join(lines))

    def _help_cmd(self):
        self._send(
            "Команды — со слэшем, в TeamTalk шли их боту в личку.\n"
            "Ссылку можно просто вставить боту в личку или в канал — сыграет.\n"
            "/play <запрос или ссылка> — найти и играть (голый /play — пауза/продолжить)\n"
            "/pause, /resume — пауза и продолжить\n"
            "/stop — стоп и очистить очередь, /skip — пропустить трек\n"
            "/n — следующий, /b — предыдущий (по списку или плейлисту)\n"
            "/playlist — список плейлиста постранично\n"
            "/u <url> — сыграть ссылку напрямую\n"
            "/radio — станции (/radio <номер> — запуск)\n"
            "/fav — избранное (f + — добавить текущий, f + <ссылка>, f <номер>, f - <номер>)\n"
            "/v <1-100> — громкость, /sf <сек> — перемотка (/sf -5 или /sb <сек> — назад)\n"
            "/cm — ответы в канал вкл/выкл, /vo — озвучка названий треков\n"
            "/sv yt / sv ym — сервис (YouTube / Яндекс.Музыка)\n"
            "/cn <ник> — ник, /cs <текст> — статус, /sc — сохранить настройки\n"
            "/lf <путь> — сыграть файл с диска, /dl — отдать трек файлом в канал\n"
            "/sub mus — присылать играющие треки в Telegram, /sub — входы/выходы\n"
            "/channel <путь> — сменить канал\n"
            "/status — что сейчас играет, /help — эта справка\n"
            "/rs — перезапустить бота"
        )

    # ----- events ----------------------------------------------------
    def onConnectSuccess(self):
        log("connected, logging in")
        self.connected = True
        self._login()

    def onConnectFailed(self):
        log("connect failed; exiting for restart")
        threading.Thread(target=_restart_bot_soon, daemon=True).start()

    def onConnectionLost(self):
        log("connection lost; exiting for restart")
        self.connected = False
        self.playing = False
        self.logged_in = False
        self.joined = False
        # In-process reconnect (self.connect) hangs without firing a result
        # event, so exit and let systemd Restart=always launch a fresh process;
        # run.sh waits for the tt5 TCP port before starting bot.py.
        threading.Thread(target=_restart_bot_soon, daemon=True).start()

    def onCmdMyselfLoggedOut(self):
        log("logged out")

    def onCmdMyselfKickedFromChannel(self, channelid, user):
        log("kicked from channel %s by %s" % (channelid, user.szNickname))

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_user_id = userid
        self.logged_in = True
        log("logged in as %s (user id %d)" % (self.nickname, userid))
        target = CHANNEL if CHANNEL else "/"
        cid = self.getChannelIDFromPath(target)
        if not cid:
            cid = self.getRootChannelID()
        self.play_channel_id = cid
        log("joining channel id %d (path %s)" % (cid, target))
        self.doJoinChannelByID(cid, CHANNEL_PASSWORD)
        if self.status_msg:
            self._set_status(self.status_msg)
        if START_COMMANDS:
            threading.Thread(target=self._run_startup_commands, daemon=True).start()

    def _run_startup_commands(self):
        time.sleep(3)
        for c in START_COMMANDS:
            try:
                log("startup cmd: %s" % c)
                self._handle_cmd(c, 0)
            except Exception as e:
                log("startup cmd error: %s" % e)
            time.sleep(1)

    def onCmdUserJoinedChannel(self, user):
        try:
            if user.nUserID == self.my_user_id:
                self.my_channel_id = user.nChannelID
                self._ready_time = time.time()
                log("joined channel id %d" % self.my_channel_id)
        except Exception:
            pass

    def _tt_field(self, user, name):
        """Read a User struct field, decode bytes → str, strip NUL."""
        v = getattr(user, name, "") or ""
        if isinstance(v, bytes):
            v = v.decode("utf-8", "ignore")
        return v.replace("\x00", "").strip()

    def _tg_send_notify(self, text, chat_id):
        """Send a Telegram notification in a background thread (don't block the audio loop)."""
        def _do():
            try:
                self._tg_api("sendMessage", chat_id=chat_id, text=text)
                log("tg notify -> %s: %s" % (chat_id, text[:80]))
            except Exception as e:
                log("tg notify err: %s" % str(e)[:120])
        threading.Thread(target=_do, daemon=True).start()

    def _load_subs(self):
        try:
            with open(SUBS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            now = time.time()
            self.sub_pending = {
                t: p for t, p in (d.get("pending") or {}).items()
                if p.get("created", 0) > now - SUB_TTL_SEC
            }
            self.sub_active = {str(c): s for c, s in (d.get("active") or {}).items()}
        except Exception:
            self.sub_pending = {}
            self.sub_active = {}

    def _save_subs(self):
        try:
            now = time.time()
            self.sub_pending = {
                t: p for t, p in self.sub_pending.items()
                if p.get("created", 0) > now - SUB_TTL_SEC
            }
            with open(SUBS_FILE, "w", encoding="utf-8") as f:
                json.dump({"pending": self.sub_pending, "active": self.sub_active},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("subs save err: %s" % e)

    def _load_admins(self):
        """Админы Telegram из users.db (владелец из конфига — админ всегда)."""
        try:
            with open(ADMINS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return [int(a) for a in (d.get("admins") or []) if str(a).strip()]
        except Exception:
            return []

    def _save_admins(self):
        try:
            with open(ADMINS_FILE, "w", encoding="utf-8") as f:
                json.dump({"admins": self.admins}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("admins save err: %s" % e)

    def _load_bans(self):
        """Баны, выданные ботом через Telegram: userid -> {nick, ip, username, banned_at}."""
        try:
            with open(BANS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return {str(k): v for k, v in (d.get("bans") or {}).items()}
        except Exception:
            return {}

    def _save_bans(self):
        try:
            with open(BANS_FILE, "w", encoding="utf-8") as f:
                json.dump({"bans": self.bans}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("bans save err: %s" % e)

    # ---- двухсторонние реплики: пересланные в Telegram сообщения TeamTalk ----

    def _load_replies(self):
        """tg_message_id -> {tt_user_id, username, nick, created_at, last_used_at}."""
        try:
            with open(REPLIES_FILE, encoding="utf-8") as f:
                d = json.load(f)
            return {str(k): v for k, v in (d.get("replies") or {}).items()}
        except Exception:
            return {}

    def _save_replies(self):
        try:
            with open(REPLIES_FILE, "w", encoding="utf-8") as f:
                json.dump({"replies": self.pending_replies}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log("replies save err: %s" % e)

    def _prune_replies(self):
        """Выкидываем протухшие pending-реплики (TTL 1 час, как в sender-rs)."""
        now = time.time()
        drop = [mid for mid, r in list(self.pending_replies.items())
                if now - (r.get("last_used_at") or r.get("created_at") or 0) > REPLY_TTL_SEC]
        for mid in drop:
            del self.pending_replies[mid]

    def _forward_recipients(self):
        """Куда пересылать сообщения из TeamTalk: notify-чат владельца + лички всех админов."""
        chats = set()
        if TG_NOTIFY_CHAT_ID:
            chats.add(int(TG_NOTIFY_CHAT_ID))
        for uid in self._tg_admin_ids():
            chats.add(int(uid))
        return chats

    def _tg_forward_user_msg(self, text, chat_id):
        """Отправить сообщение в Telegram и вернуть его message_id (для pending-reply)."""
        try:
            res = self._tg_api("sendMessage", chat_id=chat_id, text=text[:4000])
            return res["result"]["message_id"]
        except Exception as e:
            log("tg forward err: %s" % str(e)[:120])
            return 0

    def _tt_forward_private(self, from_uid, text):
        """Переслать личное сообщение TeamTalk админам в Telegram и запомнить для ответа."""
        try:
            u = self.users.get(from_uid)
            nick = self._tt_field(u, "szNickname") if u else ""
            uname = self._tt_field(u, "szUsername") if u else ""
            who = nick or uname or "id %s" % from_uid
            body = "ЛС TeamTalk от %s: %s" % (who, text)
            for chat in self._forward_recipients():
                mid = self._tg_forward_user_msg(body, chat)
                if not mid:
                    continue
                self.pending_replies[str(mid)] = {
                    "tt_user_id": from_uid, "username": uname, "nick": nick,
                    "created_at": time.time(), "last_used_at": time.time(),
                }
                log("forwarded PM from %d -> tg msg %d" % (from_uid, mid))
            self._save_replies()
        except Exception as e:
            log("forward private err: %s" % str(e)[:150])

    def _send_to_tt_user(self, uid, text):
        """Отправить личное сообщение конкретному пользователю TeamTalk (MSGTYPE_USER)."""
        if not self.logged_in or not uid:
            return False
        try:
            msgs = buildTextMessage(text, TextMsgType.MSGTYPE_USER, nToUserID=uid)
            for m in msgs:
                self.doTextMessage(m)
            return True
        except Exception as e:
            log("send to tt user %d err: %s" % (uid, str(e)[:150]))
            return False

    def _send_network_msg(self, text):
        """Отправить сетевое сообщение (broadcast) всем подключённым на сервере."""
        if not self.logged_in or not text:
            return False
        try:
            msgs = buildTextMessage(text, TextMsgType.MSGTYPE_BROADCAST, 0)
            for m in msgs:
                self.doTextMessage(m)
            return True
        except Exception as e:
            log("send network msg err: %s" % str(e)[:150])
            return False

    def _is_tt_command(self, text):
        """Команда боту только со слэшем: /play, /sub, ... Всё остальное — сообщение
        (пересылается в Telegram)."""
        return bool(text and text.startswith("/"))

    def _is_typing_indicator(self, msg):
        """Клиенты TeamTalk, пока пользователь набирает текст, шлют в канал текст
        «typing\nN» — это индикатор набора, не настоящее сообщение. Его не
        пересылаем и не обрабатываем, иначе засоряет чат в Telegram."""
        low = msg.strip().lower()
        return low == "typing" or low.startswith("typing\n")

    def _bare_link(self, text):
        """Вернуть URL, если сообщение — ровно одна ссылка и ничего больше."""
        t = (text or "").strip()
        m = URL_RE.match(t)
        if m and m.group(0) == t:
            return t
        return None

    def _tg_admin_ids(self):
        ids = set(self.admins)
        if TG_OWNER_USER_ID:
            ids.add(TG_OWNER_USER_ID)
        return ids

    def _is_admin(self, from_user):
        """TeamTalk: админ = USERTYPE_ADMIN (2) на сервере. Telegram-путь (from_user=0)
        уже отфильтрован в _tg_allowed, поэтому проходит."""
        if not from_user:
            return True
        u = self.users.get(from_user)
        return bool(u and (int(getattr(u, "uUserType", 0) or 0) & 2))  # USERTYPE_ADMIN — бит

    def _admins_text(self):
        ids = sorted(self._tg_admin_ids())
        lines = ["Администраторы (%d):" % len(ids)]
        for i in ids:
            lines.append("%s%s" % (i, "  (владелец)" if i == TG_OWNER_USER_ID else ""))
        return "\n".join(lines)

    def _tg_admin_cmd(self, msg, text):
        """/admin <user_id> — назначить админа (только владелец), пишется в users.db."""
        cid = (msg.get("chat") or {}).get("id")
        uid = (msg.get("from") or {}).get("id")
        if uid != TG_OWNER_USER_ID:
            self._tg_send_text(cid, "Назначать админов может только владелец.")
            return
        parts = text.split()
        if len(parts) < 2 or not parts[1].lstrip("+-").isdigit():
            self._tg_send_text(cid, "Формат: /admin <user_id>")
            return
        aid = int(parts[1])
        if aid in self.admins:
            self._tg_send_text(cid, "%s уже админ." % aid)
            return
        self.admins.append(aid)
        self._save_admins()
        self._tg_register_commands()
        self._tg_notify_promoted(aid)
        self._tg_send_text(cid, "✅ Админ назначен: %s (users.db обновлён)." % aid)

    def _tg_notify_promoted(self, aid):
        """Уведомить в Telegram человека, которого только что назначили админом."""
        self._tg_send_text(aid, "Тебя назначили админом бота. Теперь доступны "
                                "админские команды: /admins, /subs, /admin, /unadmin, /delsub.")

    def _tg_unadmin_cmd(self, msg, text):
        """/unadmin <user_id> — снять админа (только владелец)."""
        cid = (msg.get("chat") or {}).get("id")
        uid = (msg.get("from") or {}).get("id")
        if uid != TG_OWNER_USER_ID:
            self._tg_send_text(cid, "Снимать админов может только владелец.")
            return
        parts = text.split()
        if len(parts) < 2 or not parts[1].lstrip("+-").isdigit():
            self._tg_send_text(cid, "Формат: /unadmin <user_id>")
            return
        aid = int(parts[1])
        if aid == TG_OWNER_USER_ID:
            self._tg_send_text(cid, "Владельца снять нельзя.")
            return
        if aid in self.admins:
            self.admins.remove(aid)
            self._save_admins()
            self._tg_register_commands()
            self._tg_send_text(cid, "Админ снят: %s (users.db обновлён)." % aid)
        else:
            self._tg_send_text(cid, "%s не админ." % aid)

    def _tg_delsub_cmd(self, msg, text):
        """/delsub <user_id> — убрать подписку подписчика (только админы)."""
        cid = (msg.get("chat") or {}).get("id")
        parts = text.split()
        if len(parts) < 2 or not parts[1].lstrip("+-").isdigit():
            self._tg_send_text(cid, "Формат: /delsub <user_id>")
            return
        sid = str(int(parts[1]))
        if sid in self.sub_active:
            del self.sub_active[sid]
            self._save_subs()
            self._tg_send_text(cid, "Подписка удалена: %s." % sid)
        else:
            self._tg_send_text(cid, "Нет подписчика с id %s." % sid)

    def _tg_bot_username(self):
        if self._tg_username:
            return self._tg_username
        try:
            res = self._tg_api("getMe")
            u = ((res or {}).get("result") or {}).get("username")
            if u:
                self._tg_username = u
        except Exception as e:
            log("getMe err: %s" % str(e)[:100])
        return self._tg_username

    def _sub_cmd(self):
        """/sub в TeamTalk: выдаём индивидуальную ссылку-подписку (только в личку)."""
        if not (TG_TOKEN and self.logged_in):
            return
        if not self.reply_user_id:
            self._send("Отправь /sub личным сообщением боту.")
            return
        username = self._tg_bot_username()
        if not username:
            self._send("Telegram-бот не настроен (нет токена).")
            return
        user = self.users.get(self.reply_user_id)
        nick = self._tt_field(user, "szNickname") if user else ""
        uname = self._tt_field(user, "szUsername") if user else ""
        token = "sub_%s" % secrets.token_hex(8)
        self.sub_pending[token] = {
            "nick": nick, "username": uname,
            "nUserID": self.reply_user_id, "created": time.time(),
        }
        self._save_subs()
        link = "https://t.me/%s?start=%s" % (username, token)
        # ссылку — только в личку, не зеркалим в канал (чтобы токен не утёк)
        was = self.channel_msg
        self.channel_msg = False
        try:
            self._send("Подписка на уведомления о входе/выходе. Открой ссылку в Telegram:\n%s" % link)
        finally:
            self.channel_msg = was

    def _sub_music_cmd(self):
        """sub mus в TeamTalk: ссылка на подписку на музыку (через отдельный музыкальный бот)."""
        if not TG_MUSIC_TOKEN or not self.logged_in:
            self._send("Музыкальный бот не настроен.")
            return
        if not self.reply_user_id:
            self._send("Отправь sub mus личным сообщением боту.")
            return
        username = self._music_bot_username()
        if not username:
            self._send("Музыкальный бот недоступен.")
            return
        user = self.users.get(self.reply_user_id)
        nick = self._tt_field(user, "szNickname") if user else ""
        uname = self._tt_field(user, "szUsername") if user else ""
        token = "sub_mus_%s" % secrets.token_hex(8)
        self.mus_pending[token] = {
            "nick": nick, "username": uname,
            "nUserID": self.reply_user_id, "created": time.time(),
        }
        self._save_music_subs()
        link = "https://t.me/%s?start=%s" % (username, token)
        # ссылку — только в личку, не зеркалим в канал (чтобы токен не утёк)
        was = self.channel_msg
        self.channel_msg = False
        try:
            self._send("Подписка на музыку: треки будут приходить в Telegram. Открой ссылку:\n%s" % link)
        finally:
            self.channel_msg = was

    def _server_name(self):
        if TG_NOTIFY_SERVER:
            return TG_NOTIFY_SERVER
        try:
            sp = self.getServerProperties()
            if sp:
                name = self._tt_field(sp, "szServerName")
                if name:
                    return name
        except Exception:
            pass
        return "TeamTalk"

    # Пол в статус-режиме (nStatusMode): бит 0x100 — женский, 0x1000 — нейтральный (TeamTalk).
    _STATUSMODE_GENDER_MASK = 0x1100
    _STATUSMODE_FEMALE = 0x0100

    def _user_female(self, user):
        """True, если у пользователя в статус-режиме стоит женский пол."""
        try:
            return (user.nStatusMode & self._STATUSMODE_GENDER_MASK) == self._STATUSMODE_FEMALE
        except Exception:
            return False

    def _notify_join_leave(self, sign, user):
        """Announce a user logging in (+)/out (-): владельцу и всем подписчикам."""
        try:
            if not TG_TOKEN or not user:
                return
            if user.nUserID == self.my_user_id:
                return
            # grace period: don't broadcast the initial roster replay right after connect
            if not self._ready_time or time.time() - self._ready_time < 3:
                return
            nick = self._tt_field(user, "szNickname") or self._tt_field(user, "szUsername")
            if not nick:
                return
            uname = self._tt_field(user, "szUsername").lower()
            if uname in TG_NOTIFY_IGNORE:
                return
            female = self._user_female(user)
            if sign == "+":
                text = "%s %s к серверу %s" % (nick, "присоединилась" if female else "присоединился", self._server_name())
            else:
                text = "%s %s сервер %s" % (nick, "покинула" if female else "покинул", self._server_name())
            if TG_NOTIFY_CHAT_ID:
                self._tg_send_notify(text, TG_NOTIFY_CHAT_ID)
            for cid in list(self.sub_active):
                self._tg_send_notify(text, int(cid))
        except Exception as e:
            log("notify join/leave err: %s" % str(e)[:150])

    def _ip_geo(self, ip):
        """Страна и город по IP через ip-api.com (бесплатный эндпоинт, без ключа)."""
        try:
            if not ip or ip in ("0.0.0.0", "::", "::1", "127.0.0.1", "localhost"):
                return ""
            url = "http://ip-api.com/json/%s?fields=status,country,city" % ip
            with urllib.request.urlopen(url, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            if data.get("status") != "success":
                return ""
            return ", ".join(p for p in (data.get("country") or "", data.get("city") or "") if p)
        except Exception as e:
            log("ip geo err: %s" % str(e)[:100])
            return ""

    def _welcome_join(self, user):
        """При входе пользователя на сервер — приветствие с ником, IP и гео в канал TeamTalk."""
        try:
            if not self.logged_in or not user or not self.my_channel_id:
                return
            if user.nUserID == self.my_user_id:
                return
            if not self._ready_time or time.time() - self._ready_time < 3:
                return
            nick = self._tt_field(user, "szNickname") or self._tt_field(user, "szUsername")
            if not nick:
                return
            uname = self._tt_field(user, "szUsername").lower()
            if uname in TG_NOTIFY_IGNORE:
                return
            ip = self._tt_field(user, "szIPAddress") or ""
            threading.Thread(target=self._welcome_do, args=(nick, ip), daemon=True).start()
        except Exception as e:
            log("welcome join err: %s" % str(e)[:150])

    def _welcome_do(self, nick, ip):
        """Гео-резолв и отправка приветствия сетевым сообщением (всем на сервере,
        в любом канале). В фоне — не блокирует событийный цикл."""
        try:
            geo = self._ip_geo(ip)
            text = "👋 Привет, %s! Добро пожаловать на сервер %s." % (nick, self._server_name())
            if ip:
                text += "\nIP: %s%s" % (ip, (" (%s)" % geo) if geo else "")
            text += "\n%s" % (WELCOME_RULES or "Ознакомься, пожалуйста, с правилами сервера.")
            self._send_network_msg(text)
        except Exception as e:
            log("welcome do err: %s" % str(e)[:150])

    # ======================= авто-защита от ботнетов =======================
    # Проверяются ТОЛЬКО гостевые/публичные входы (имя задаётся в конфиге
    # guard.guest_logins — у владельца это «1», у других может быть
    # «guest», «public» или пустая анонимная). Всем остальным учёткам бот
    # доверяет: владельца с VPN и друзей не блокируем. Для гостя срабатывают:
    # гео страны IP (по умолчанию только РФ), ботнет-ник, слишком длинный ник,
    # нецензурный ник, пустой ник, всплеск массовых входов. Длина/мат считаются
    # ДО гео — ловят простыни в нике даже с российских адресов. Сработавших
    # кикаем и, если можно, баним IP, плюс шлём агрегированный алерт в Telegram.

    def _prot_read_cfg(self):
        def _g(node, path, default):
            for p in path.split("."):
                if isinstance(node, dict) and p in node:
                    node = node[p]
                else:
                    return default
            return node if node not in (None, "", [], {}) else default

        p = _g(CFG, "guard", {}) or {}
        geo = _g(p, "geo", {}) or {}
        burst = _g(p, "burst", {}) or {}
        wl = _g(p, "trusted", {}) or {}
        rf = _g(geo, "ranges_file", "geo/ru_ipv4.txt") or "geo/ru_ipv4.txt"
        if not os.path.isabs(rf):
            rf = os.path.join(BASE_DIR, rf)
        return {
            "enabled": bool(_g(p, "enabled", True)),
            "guest_usernames": {str(u).strip().lower() for u in (_g(p, "guest_logins", []) or [])},
            "notify": bool(_g(p, "notify", True)),
            "geo_enabled": bool(_g(geo, "enabled", True)),
            "allow_countries": [str(c).upper() for c in (_g(geo, "allow_countries", ["RU"]) or ["RU"])],
            "ranges_file": rf,
            "nick_enabled": bool(_g(p, "botnet_nick_check", True)),
            "empty_nick_enabled": bool(_g(p, "kick_empty_nick", True)),
            "nick_max_len": int(_g(p, "max_nick_len", _DEFAULT_NICK_MAX_LEN) or 0),
            "mat_enabled": bool(_g(p, "mat_check", True)),
            "mat_re": _compile_mat_re(_g(p, "mat_words", _DEFAULT_MAT_ROOTS) or _DEFAULT_MAT_ROOTS),
            "burst_enabled": bool(_g(p, "burst_enabled", True)),
            "burst_window": float(_g(burst, "window_sec", 30) or 30),
            "burst_threshold": int(_g(burst, "threshold", 20) or 20),
            "burst_cooldown": float(_g(burst, "cooldown_sec", 300) or 300),
            "wl_usernames": {str(u).strip().lower() for u in (_g(wl, "usernames", []) or []) if str(u).strip()},
            "wl_nicknames": [str(n).strip().lower() for n in (_g(wl, "nicknames", []) or []) if str(n).strip()],
            "wl_ips": [str(i).strip() for i in (_g(wl, "ip_prefixes", []) or []) if str(i).strip()],
        }

    def _prot_load_ranges(self):
        """Загружает офлайн-CIDR список стран в отсортированные массивы int."""
        try:
            path = self._prot_cfg["ranges_file"]
            starts, ends = [], []
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.split("#")[0].strip()
                    if not line:
                        continue
                    if "/" not in line:
                        line += "/32"
                    try:
                        net = ipaddress.ip_network(line, strict=False)
                    except Exception:
                        continue
                    if net.version != 4:
                        continue
                    starts.append(int(net.network_address))
                    ends.append(int(net.broadcast_address))
            order = sorted(range(len(starts)), key=lambda i: starts[i])
            self._ru_starts = [starts[j] for j in order]
            self._ru_ends = [ends[j] for j in order]
            log("protection: RU ranges loaded: %d" % len(self._ru_starts))
        except Exception as e:
            log("protection: geo load err: %s" % str(e)[:150])
            self._ru_starts = []
            self._ru_ends = []

    @staticmethod
    def _prot_v4int(ip):
        """Возвращает int для IPv4 (в т.ч. IPv4-mapped «::ffff:a.b.c.d»), иначе None."""
        s = ip.split("%")[0].strip()
        if not s:
            return None
        try:
            return int(ipaddress.IPv4Address(s))
        except Exception:
            pass
        try:
            a = ipaddress.ip_address(s)
        except Exception:
            return None
        if isinstance(a, ipaddress.IPv4Address):
            return int(a)
        if isinstance(a, ipaddress.IPv6Address):
            m = getattr(a, "ipv4_mapped", None)
            if m is not None:
                return int(m)
        return None

    def _ip_in_ru(self, ip):
        """True, если ip входит в RU-подсети (двоичный поиск по сортировке)."""
        if not ip or not self._ru_starts:
            return False
        num = self._prot_v4int(ip)
        if num is None:
            return False
        i = bisect.bisect_right(self._ru_starts, num) - 1
        return i >= 0 and num <= self._ru_ends[i]

    @staticmethod
    def _nick_wl(nl, pref):
        """Ник в белом списке: точное совпадение или префикс до границы слова
        (пробел/дефис/скобки), чтобы «kirill» не пускал «kirill_bot»."""
        if nl == pref:
            return True
        if not nl.startswith(pref):
            return False
        return nl[len(pref):len(pref) + 1] in (" ", "-", "(", "[", "")

    def _prot_is_admin(self, user, uid):
        """Сам бот или серверный админ (uUserType 2) — всегда доверяем."""
        if uid == self.my_user_id:
            return True
        return bool(int(getattr(user, "uUserType", 0) or 0) & 2)

    def _prot_is_guest_login(self, username):
        """Гостевая/публичная учётка — ЕДИНСТВЕННЫЕ, кого проверяем.
        Пустое имя пользователя (анонимный гость) и учётки из
        guard.guest_logins (у владельца это «1», у других серверов
        может быть «guest», «public» или пустая). Все прочие учётные записи
        бот доверяет без проверок — флуд ходит именно через гостевую."""
        u = (username or "").strip()
        if not u:
            return True
        return u.lower() in self._prot_cfg["guest_usernames"]

    def _prot_in_whitelist(self, username, nick, ip):
        """Дополнительный пропуск даже на гостевой учётке (по имени/нику/IP)."""
        if (username or "").lower() in self._prot_cfg["wl_usernames"]:
            return True
        nl = (nick or "").lower()
        for pref in self._prot_cfg["wl_nicknames"]:
            if self._nick_wl(nl, pref):
                return True
        if ip:
            for p in self._prot_cfg["wl_ips"]:
                if ip.startswith(p):
                    return True
        return False

    def _prot_decide(self, nick, username, ip):
        """Первая сработавшая проверка. Возвращает (reason, kick_only);
        (None, False) — вход разрешён."""
        if self._prot_cfg["empty_nick_enabled"] and not nick:
            return ("пустой ник (учётка «%s»)" % (username or "?"), True)
        if self._prot_cfg["nick_enabled"] and nick and _is_botnet_nick(nick):
            return ("ботнет-ник «%s»" % nick[:28], False)
        mlen = self._prot_cfg["nick_max_len"]
        if nick and mlen and len(nick) > mlen:
            return ("слишком длинный ник «%s» (%d симв.)" % (nick[:24], len(nick)), False)
        mre = self._prot_cfg["mat_re"]
        if nick and self._prot_cfg["mat_enabled"] and mre and \
                mre.search(nick.lower().replace("ё", "е")):
            return ("нецензурный ник «%s»" % nick[:24], False)
        if self._prot_burst_until > time.time():
            return ("всплеск входов (режим защиты)", False)
        if self._prot_cfg["geo_enabled"] and ip:
            if not self._ru_starts:
                # RU-список не загрузился — не рубим всех подряд (fail-open),
                # предупреждаем один раз; ник/всплеск продолжают работать
                if not self._prot_geo_off_warned:
                    self._prot_geo_off_warned = True
                    self._prot_note("нет загруженного RU-списка — гео-проверка отключена")
                return (None, False)
            if not self._ip_in_ru(ip):
                extras = [c for c in self._prot_cfg["allow_countries"] if c != "RU"]
                if not extras:
                    return ("IP %s не из РФ (ник «%s»)" % (ip, nick or username or "?"), False)
        return (None, False)

    def _prot_burst_track(self, ip):
        """Скользящее окно недавних входов; при N разных IP за окно включает
        режим всплеска на cooldown."""
        if not self._prot_cfg["burst_enabled"] or not ip:
            return
        now = time.time()
        win = self._prot_cfg["burst_window"]
        self._prot_recent.append((now, ip))
        while self._prot_recent and now - self._prot_recent[0][0] > win:
            self._prot_recent.popleft()
        if now < self._prot_burst_until:
            return
        ips = {p[1] for p in self._prot_recent if p[1]}
        if len(ips) >= self._prot_cfg["burst_threshold"]:
            self._prot_burst_until = now + self._prot_cfg["burst_cooldown"]
            self._prot_note("всплеск входов: %d разных IP за %dс — режим защиты на %dс"
                            % (len(ips), int(win), int(self._prot_cfg["burst_cooldown"])))
            log("protection: burst tripped (%d IPs)" % len(ips))

    def _prot_check_login(self, user):
        """Хук из onCmdUserLoggedIn: на КАЖДЫЙ вход (в т.ч. реплей после
        рестарта бота). Вердикт получает только гостевая/публичная учётка."""
        try:
            if not self._prot_cfg.get("enabled") or not user or not self.logged_in:
                return
            try:
                uid = int(getattr(user, "nUserID", 0) or 0)
            except Exception:
                uid = 0
            if uid <= 0:
                return
            ip = self._tt_field(user, "szIPAddress") or ""
            nick = self._tt_field(user, "szNickname") or ""
            username = self._tt_field(user, "szUsername") or ""
            if self._prot_is_admin(user, uid):
                return  # сам бот и админы — доверяем всегда (владелец под VPN)
            if not self._prot_is_guest_login(username):
                return  # не гостевая учётка — доверяем, не проверяем
            if self._prot_in_whitelist(username, nick, ip):
                return  # явный пропуск даже на гостевой
            self._prot_burst_track(ip)
            reason, kick_only = self._prot_decide(nick, username, ip)
            if not reason:
                return
            self._prot_bad[uid] = reason
            self._prot_q.put((uid, nick, username, ip, reason, kick_only))
            if self._prot_worker is None or not self._prot_worker.is_alive():
                self._prot_worker = threading.Thread(target=self._prot_ban_loop,
                                                     daemon=True, name="prot-ban")
                self._prot_worker.start()
        except Exception as e:
            log("protection check err: %s" % str(e)[:150])

    def _prot_ban_loop(self):
        """Серийный банильщик: один поток, очередь — чтобы флуд не плодил тысячи
        потоков и не долбил диск одновременными save_bans."""
        while True:
            try:
                uid, nick, username, ip, reason, kick_only = self._prot_q.get(timeout=4)
            except queue.Empty:
                self._prot_worker = None
                return
            try:
                self._prot_ban_one(uid, nick, username, ip, reason, kick_only)
            except Exception as e:
                log("protection ban err: %s" % str(e)[:150])
            finally:
                self._prot_q.task_done()

    def _prot_ban_one(self, uid, nick, username, ip, reason, kick_only):
        try:
            self.doKickUser(uid, 0)
        except Exception as e:
            log("protection kick err: %s" % str(e)[:120])
        if kick_only or not ip:
            self._prot_note(reason)
            return
        if ip in self._banned_ips:
            self._prot_note(reason)  # IP уже в бане — считаем событие, банить нечего
            return
        try:
            self.doBanIPAddress(_b(ip), 0)
        except Exception as e:
            log("protection ipban err: %s" % str(e)[:120])
            self._prot_note("не забанить %s: %s" % (ip, str(e)[:90]))
            return
        self._banned_ips.add(ip)
        self.bans[str(uid)] = {
            "nick": nick,
            "username": username,
            "ip": ip,
            "banned_at": time.time(),
            "auto": reason,
        }
        self._trim_bans()
        self._save_bans()
        self._prot_note(reason)

    def _trim_bans(self):
        """Авто-баны не копим бесконечно: свыше 1200 записей режем старые авто до 800."""
        try:
            if len(self.bans) <= 1200:
                return
            auto = sorted(
                ((v.get("banned_at") or 0, k) for k, v in self.bans.items()
                 if isinstance(v, dict) and v.get("auto")),
                key=lambda kv: kv[0],
            )
            for _, k in auto[:max(0, len(auto) - 800)]:
                self.bans.pop(k, None)
        except Exception:
            pass

    def _prot_note(self, key):
        """Копит событие защиты в счётчик; флушер шлёт агрегированный репорт
        в Telegram (не спамим на каждый кик)."""
        try:
            if not self._prot_cfg.get("notify"):
                return
            with self._prot_lock:
                self._prot_counts[key] = self._prot_counts.get(key, 0) + 1
                if self._prot_flush is None or not self._prot_flush.is_alive():
                    self._prot_flush = threading.Thread(target=self._prot_flush_loop,
                                                        daemon=True, name="prot-flush")
                    self._prot_flush.start()
        except Exception:
            pass

    def _prot_flush_loop(self):
        while True:
            time.sleep(10)
            with self._prot_lock:
                if not self._prot_counts:
                    self._prot_flush = None
                    return
            if time.time() - self._prot_last_report < 30:
                continue
            with self._prot_lock:
                counts = self._prot_counts
                self._prot_counts = {}
                self._prot_last_report = time.time()
            self._prot_send_report(counts)

    def _prot_send_report(self, counts):
        try:
            if not counts:
                return
            items = sorted(counts.items(), key=lambda kv: -kv[1])
            lines = ["- %s: %d" % (k, v) for k, v in items[:8]]
            rest = sum(v for _, v in items[8:])
            if rest:
                lines.append("- и ещё %d событий" % rest)
            text = "Защита сервера %s:\n%s" % (self._server_name(), "\n".join(lines))
            target = TG_NOTIFY_CHAT_ID or TG_OWNER_USER_ID
            if target:
                self._tg_send_notify(text, int(target))
        except Exception as e:
            log("protection report err: %s" % str(e)[:120])

    def onCmdUserLoggedIn(self, user):
        try:
            self.users[user.nUserID] = user
        except Exception:
            pass
        self._prot_check_login(user)
        if user and getattr(user, "nUserID", 0) in self._prot_bad:
            return  # забанили — не анонсируем и не шлём приветствие ботнету
        self._notify_join_leave("+", user)
        self._welcome_join(user)

    def onCmdUserLoggedOut(self, user):
        try:
            self.users.pop(user.nUserID, None)
        except Exception:
            pass
        try:
            if user and user.nUserID in self._prot_bad:
                self._prot_bad.pop(user.nUserID, None)
                return
        except Exception:
            pass
        self._notify_join_leave("-", user)

    def _dl_finish(self, ok, detail=""):
        if self._dl_local:
            try:
                os.remove(self._dl_local)
            except Exception:
                pass
            self._dl_local = None
        name = self._dl_remote or "трек"
        self._dl_cmd_id = None
        self._dl_remote = None
        if ok:
            self._send("✅ Файл «%s» загружен в канал — можно скачать в TeamTalk." % name)
        else:
            self._send("⚠ Не удалось загрузить файл в канал: %s" % (detail or "неизвестная ошибка"))

    def onCmdSuccess(self, cmdId):
        if self._dl_cmd_id is not None and cmdId == self._dl_cmd_id:
            self._dl_finish(True)

    def onCmdError(self, cmdId, errmsg):
        msg = errmsg.szErrorMsg if errmsg else ""
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "ignore")
        log("cmd error (cmd %d): %s" % (cmdId, msg))
        if self._dl_cmd_id is not None and cmdId == self._dl_cmd_id:
            self._dl_finish(False, msg)
            return
        now = time.time()
        if now - self._last_err_sent > 5:
            self._last_err_sent = now
            self._send("⚠ Ошибка: %s" % msg)

    def onCmdUserTextMessage(self, textmessage):
        if not textmessage:
            return
        if textmessage.nFromUserID == self.my_user_id:
            return
        try:
            msg = textmessage.szMessage
            if isinstance(msg, bytes):
                msg = msg.decode("utf-8", "ignore")
            msg = msg.replace("\x00", "").strip()
            if not msg:
                return
            log("msg from %d: %s" % (textmessage.nFromUserID, msg))
            if self._is_typing_indicator(msg):
                log("typing indicator from %d, skip" % textmessage.nFromUserID)
                return
            # команды — только со слэшем (/sub, /play, ...). Исключение:
            # сообщение-ссылка (ровно один URL и больше ничего) — в личку боту
            # или в канал вставленную ссылку играем, как /play <ссылка>.
            # Прочие не-команды: в личку — пересылаем админам (можно ответить),
            # в канал — игнорируем (пересылка только засоряла чат)
            if not self._is_tt_command(msg):
                bare = self._bare_link(msg)
                if bare:
                    self._handle_cmd("/play " + bare, textmessage.nFromUserID)
                    return
                if int(getattr(textmessage, "nMsgType", 0) or 0) == TextMsgType.MSGTYPE_USER:
                    self._tt_forward_private(textmessage.nFromUserID, msg)
                return
            self._handle_cmd(msg, textmessage.nFromUserID)
        except Exception as e:
            log("handle msg error: %s" % e)

    def onStreamMediaFile(self, mediafileinfo):
        try:
            status = mediafileinfo.nStatus
            if status in (
                MediaFileStatus.MFS_FINISHED,
                MediaFileStatus.MFS_CLOSED,
                MediaFileStatus.MFS_ABORTED,
                MediaFileStatus.MFS_ERROR,
            ):
                if self.playing:
                    self.api_q.put(("advance",))
        except Exception as e:
            log("stream event error: %s" % e)

    # ----- lifecycle --------------------------------------------------
    def _login(self):
        self.doLogin(self.nickname, USERNAME, PASSWORD, CLIENTNAME)

    def _run_once(self):
        self.runEventLoop(50)
        while True:
            try:
                item = self.api_q.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            try:
                if kind == "download_ok":
                    _, url, title, path = item
                    if url in self.downloading:
                        self.downloading.discard(url)
                    if self.queue and self.queue[0][0] == url:
                        self.queue[0] = (url, title)
                        if not self.playing:
                            self._play_file(url, title, path)
                        elif not self.silent:
                            self._send("⬇ Скачано (в очереди): %s" % title)
                    # else: stale download (superseded by n/b switch) — drop
                elif kind == "download_fail":
                    _, url, title, err = item
                    if url in self.downloading:
                        self.downloading.discard(url)
                    # remove from queue if first
                    if self.queue and self.queue[0][0] == url:
                        self.queue.pop(0)
                        if self.auto_playlist and self.playlist:
                            self._send("⚠ Не удалось: %s" % err)
                            self._advance(silent=True)
                        else:
                            self._send("⚠ Не удалось скачать %s: %s" % (url, err))
                            self._enqueue_next()
                    else:
                        self._send("⚠ Ошибка скачивания: %s" % err)
                elif kind == "search_done":
                    _, query, items = item
                    if not items:
                        self._send("Ничего не нашёл по запросу «%s»." % query)
                        return
                    self.search_results = items
                    self.search_index = 0
                    lines = ["Результаты (%d):" % len(items)]
                    for i, it in enumerate(items[:10], 1):
                        lines.append("%d. %s" % (i, it["title"][:60]))
                    self._send("\n".join(lines))
                    self._play_search_index(0)
                elif kind == "playlist_done":
                    _, url, items = item
                    if not items:
                        self._send("⚠ Не удалось получить плейлист: %s" % url)
                        return
                    self.auto_list = False
                    self.queue = []
                    self.downloading.clear()
                    self._stop_voice()
                    self.playing = False
                    self.paused = False
                    self.current = None
                    self.current_orig = None
                    self.cur_offset_ms = 0
                    self.voice_offset_base = 0
                    self.voice_started_at = 0
                    self._set_status(self.status_msg)
                    self.playlist = items
                    self._send("\n".join(self._playlist_page_lines(1)))
                    self._play_playlist_index(0)
                elif kind == "advance":
                    self._advance(silent=True)
                elif kind == "voice_finished":
                    if self._announce_pending:
                        path, offset = self._announce_pending
                        self._announce_pending = None
                        self._start_voice(path, offset)
                    else:
                        self._advance(silent=True)
                elif kind == "voice_error":
                    _, msg = item
                    self._send("⚠ Голос: %s" % msg)
                    self._advance()
                elif kind == "status":
                    _, msg = item
                    self._send(msg)
                elif kind == "local_file":
                    _, path, title = item
                    self._play_local(path, title)
                elif kind == "restart":
                    _, path, offset = item
                    self._start_voice(path, int(offset))
            except Exception as e:
                log("api_q handler error: %s\n%s" % (e, traceback.format_exc()))

    def run(self):
        while self.reconnect:
            try:
                self.connect(HOST, TCP_PORT, UDP_PORT)
                break
            except Exception as e:
                log("connect exception: %s\n%s" % (e, traceback.format_exc()))
                time.sleep(10)
        # The SDK's connect is async and sometimes stalls with no result
        # event (TCP established, handshake never completes). Watchdog:
        # if not connected within 20s, exit and let systemd relaunch.
        deadline = time.monotonic() + CONNECT_TIMEOUT
        while self.reconnect and not self.connected:
            self.runEventLoop(100)
            if time.monotonic() > deadline:
                log("connect watchdog timeout; exiting for restart")
                threading.Thread(target=_restart_bot_soon, daemon=True).start()
                return
        while self.reconnect:
            try:
                self._run_once()
            except KeyboardInterrupt:
                break
            except Exception as e:
                log("loop error: %s" % e)
                time.sleep(1)


def main():
    bot = MusicBot()
    log("starting music bot for %s:%d (user %s)" % (HOST, TCP_PORT, USERNAME))
    _start_nightly_restart()
    _start_ydlp_updater()
    if REG_ENABLED and REG_TOKEN and REG_ADMIN_USER_IDS:
        try:
            import tt_register
            bot._registrar = tt_register.start({
                "token": REG_TOKEN,
                "admin_user_ids": REG_ADMIN_USER_IDS,
                "broadcast_text": REG_BROADCAST_TEXT,
                "hostname": HOST,
                "tcp_port": TCP_PORT,
                "udp_port": UDP_PORT,
                "tt_username": REG_ADMIN_TT_USER,
                "tt_password": REG_ADMIN_TT_PASS,
                "tt_nickname": REG_ADMIN_TT_NICK,
                "state_file": os.path.join(BASE_DIR, "register_requests.json"),
                "log_fn": log,
            })
            if bot._registrar:
                log("registrar: модуль регистрации запущен")
            else:
                log("registrar: пустой токен/админ — регистратор выключен")
        except Exception as e:
            log("registrar start error: %s" % e)
    bot.run()


if __name__ == "__main__":
    main()
