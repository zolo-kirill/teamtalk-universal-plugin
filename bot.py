#!/usr/bin/env python3
"""Music bot for TeamTalk: plays audio from URLs into a voice channel."""
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

# ---- config: JSON file in the TtMediaBot style ----
# config.json (gitignored) overrides config_default.json; env vars are a fallback.
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


HOST = str(_cfg("teamtalk.hostname", "TEAMTALK_HOST", "example.com"))
TCP_PORT = int(_cfg("teamtalk.tcp_port", "TEAMTALK_TCP_PORT", 10333))
UDP_PORT = int(_cfg("teamtalk.udp_port", "TEAMTALK_UDP_PORT", 10333))
NICKNAME = str(_cfg("teamtalk.nickname", "TEAMTALK_NICKNAME", "MusicBot"))
USERNAME = str(_cfg("teamtalk.username", "TEAMTALK_USERNAME", "example"))
PASSWORD = str(_cfg("teamtalk.password", "TEAMTALK_PASSWORD", ""))
CHANNEL = str(_cfg("teamtalk.channel", "TEAMTALK_CHANNEL", ""))  # empty = root channel
CHANNEL_PASSWORD = str(_cfg("teamtalk.channel_password", "TEAMTALK_CHANNEL_PASSWORD", ""))
DEFAULT_VOLUME = int(_cfg("player.default_volume", None, 10))
MAX_VOLUME = int(_cfg("player.max_volume", None, 100))
DEFAULT_SERVICE = str(_cfg("general.default_service", None, "yt"))
DEFAULT_CHANNEL_MSG = bool(_cfg("general.send_channel_messages", None, True))
START_COMMANDS = list(_cfg("general.start_commands", None, []))
CLIENTNAME = "teamtalk-universal-plugin"

CACHE_DIR = os.path.join(BASE_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)
INBOX_DIR = os.path.join(BASE_DIR, "inbox")  # files relayed from Telegram
os.makedirs(INBOX_DIR, exist_ok=True)

NICKNAME_FILE = os.path.join(BASE_DIR, ".nickname")
CHANNEL_MSG_FILE = os.path.join(BASE_DIR, ".channel_msg")
FAVORITES_FILE = os.path.join(BASE_DIR, "favorites.json")
SUBS_FILE = os.path.join(BASE_DIR, "subs.json")
SUB_TTL_SEC = 86400  # сколько живёт ссылка-подписка (24 ч)
ADMINS_FILE = os.path.join(BASE_DIR, "users.db")  # user id администраторов Telegram

TG_TOKEN = str(_cfg("telegram_relay.token", "TG_TOKEN", "")).strip()  # optional: own Telegram bot that relays files
TG_OWNER_USER_ID = int(_cfg("telegram_relay.owner_user_id", "TG_OWNER_USER_ID", 0) or 0)  # only this user can send commands
TG_NOTIFY_CHAT_ID = int(_cfg("telegram_relay.notify_chat_id", "TG_NOTIFY_CHAT_ID", 0) or 0)  # сюда слать вход/выход пользователей (0 = выкл)
TG_NOTIFY_SERVER = str(_cfg("telegram_relay.notify_server_name", "TG_NOTIFY_SERVER_NAME", "")).strip()  # пусто = брать имя сервера из TeamTalk
TG_NOTIFY_IGNORE = {u.strip().lower() for u in (_cfg("telegram_relay.ignore_users", None, []) or []) if u.strip()}
TG_NOTIFY_IGNORE.add("bot_admin")  # все боты на одной админ-учётке — их не анонсируем

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
    YT_JS_RUNTIME = _cfg("general.yt_js_runtime", None, None)
if not YT_JS_RUNTIME:
    YT_JS_RUNTIME = "/home/superlisa/.local/bin/deno"

# YouTube po_token provider extractor-arg; empty disables it (e.g. no bgutil server).
YT_PO_TOKEN = os.environ.get("YT_PO_TOKEN_EXTRACTOR")
if YT_PO_TOKEN is None:
    YT_PO_TOKEN = _cfg("general.yt_po_token_extractor", None, None)
    if YT_PO_TOKEN is None:
        YT_PO_TOKEN = "youtube:po_token_provider=bgutil:http"

# Max tracks loaded from a playlist (YouTube / Yandex Music). High default so big
# playlists («Мне нравится» ≈ тысячи треков) load fully; override via config.
PLAYLIST_LIMIT = int(_cfg("general.playlist_limit", None, 5000))

# Voice transmission: raw PCM fed to TT_InsertAudioBlock as STREAMTYPE_VOICE.
VOICE_RATE = 48000  # Hz
VOICE_CHUNK = 960   # samples per block (20 ms at 48 kHz)
VOICE_CHUNK_BYTES = VOICE_CHUNK * 2  # s16 mono

# Playback through PulseAudio: when set, the track is played by ffmpeg into
# this sink and captured back from its monitor, so anything audible on the
# machine can be routed into the channel. Empty = decode straight to PCM.
PULSE_SINK = _cfg("general.pulse_sink", None, "") or ""


def log(msg):
    print("%s %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def _fmt_ms(ms):
    s = int(ms) // 1000
    return "%d:%02d" % (s // 60, s % 60)


def _restart_bot_soon():
    """Exit the process shortly; the service supervisor (restart=always) relaunches."""
    time.sleep(1.5)
    log("restarting by command")
    os._exit(0)


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
        self.connecting = False
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
        try:
            n = open(NICKNAME_FILE).read().strip()
            if n:
                self.nickname = n
        except Exception:
            pass
        # reply targeting: PM to the command author, optionally mirrored to channel
        self.reply_user_id = 0  # who sent the last command → PM replies
        self.channel_msg = self._load_channel_msg()  # mirror replies to channel (cm)
        # optional Telegram relay: an own bot that forwards files and commands into the channel
        self._tg_offset = 0
        self._tg_reply_chat = None  # set while handling a Telegram command → mirror replies
        self._ready_time = None  # when the bot finished joining — for join/leave notify grace
        # подписки на уведомления: /sub в TeamTalk → ссылка → активация в Telegram
        self.users = {}  # nUserID -> User (кто сейчас на сервере)
        self._tg_username = None  # bot username from getMe, для /sub-ссылок
        self.sub_pending = {}  # token -> {nick, username, nUserID, created}
        self.sub_active = {}  # chat_id(str) -> {nick, username, nUserID, subscribed_at}
        self._load_subs()
        self.admins = self._load_admins()  # user id администраторов Telegram (users.db); владелец — всегда
        if TG_TOKEN:
            threading.Thread(target=self._tg_poll, daemon=True, name="tg-poll").start()
            threading.Thread(target=self._tg_register_commands, daemon=True, name="tg-cmds").start()
            log("telegram relay enabled")

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
            if low in ("/help", "/команды", "help", "помощь", "команды"):
                self._tg_handle_help(msg)
                return
            if low in ("/online", "online", "онлайн"):
                self._tg_send_text((msg.get("chat") or {}).get("id"), self._online_text())
                return
            # treat text as a bot command; mirror replies back to this chat
            if not self._tg_allowed(msg):
                return
            cid = (msg.get("chat") or {}).get("id")
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
            prev = self.reply_user_id
            self.reply_user_id = 0
            self._tg_reply_chat = (msg.get("chat") or {}).get("id")
            try:
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
        title = (
            msg.get("caption")
            or media.get("file_name")
            or media.get("title")
            or "audio"
        ).strip() or "audio"
        path = self._tg_download(file_id)
        if path:
            self.api_q.put(("local_file", path, title[:80]))

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
            nick = rec.get("nick") or rec.get("username") or "?"
            kb.append([{"text": "%s (id %s)" % (nick, cid),
                        "callback_data": "subs:view:%s" % cid}])
        if items:
            lines.append("Нажми на подписчика — откроются действия.")
        else:
            lines.append("Пока никто не подписан.")
        return "\n".join(lines), kb

    def _subs_view(self, cid, actor_uid):
        """Карточка подписчика: статус и кнопки действий."""
        rec = self.sub_active.get(str(cid)) or {}
        nick = rec.get("nick") or rec.get("username") or "?"
        is_admin = int(cid) in self.admins
        is_owner = str(cid) == str(TG_OWNER_USER_ID)
        lines = [
            "Подписчик: %s" % nick,
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
        log("tg unknown callback: %s" % data)

    def _tg_handle_sub_msg(self, msg, text):
        """/start (по deep-link sub_<token>) и /unsub — для подписчиков, не только владельца."""
        cid = (msg.get("chat") or {}).get("id")
        if not cid:
            return
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
        self.sub_active[str(cid)] = rec
        self._save_subs()
        who = rec.get("nick") or rec.get("username") or "твой аккаунт"
        self._tg_send_text(cid, "✅ Подписка активна (%s): будешь получать уведомления о входе/выходе на сервере «%s». Отписаться — /unsub." % (who, self._server_name()))

    def _tg_help_text(self, is_admin=False):
        text = (
            "Команды бота:\n"
            "п <запрос> / пи <запрос> — поиск и игра (YouTube / Яндекс.Музыка)\n"
            "n — следующий, b — предыдущий\n"
            "п / пи — пауза / продолжить\n"
            "с / стоп — стоп, скип / дальше — дальше\n"
            "u <ссылка> — играть по ссылке\n"
            "v <1-100> — громкость\n"
            "sf <сек> — перемотка (sf -5 — назад)\n"
            "пл <страница> — список плейлиста\n"
            "радио — радиостанции (радио <номер> — запуск)\n"
            "f — избранное (f +, f + <ссылка>, f <номер>, f - <номер>)\n"
            "sv yt / sv ym — сервис\n"
            "cm — отвечать в канал/личку\n"
            "cn <ник> — ник бота\n"
            "очередь, статус, онлайн — кто сейчас на сервере\n"
            "sub — ссылка на подписку (команда работает в TeamTalk)\n"
            "Музыку заказывает любой, управление ботом — только админам."
        )
        if is_admin:
            text += "\nДля админов: /admins, /admin <id>, /unadmin <id>, /subs, /delsub <id>"
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
            {"command": "unsub", "description": "Отписаться от уведомлений"},
            {"command": "play", "description": "Поиск и игра: /play <запрос>"},
            {"command": "online", "description": "Кто сейчас на сервере"},
            {"command": "next", "description": "Следующий трек"},
            {"command": "prev", "description": "Предыдущий трек"},
            {"command": "volume", "description": "Громкость: /volume <1-100>"},
            {"command": "favorites", "description": "Избранное: /favorites"},
            {"command": "radio", "description": "Радиостанции"},
        ]
        admin = [
            {"command": "admins", "description": "Список админов бота"},
            {"command": "admin", "description": "Назначить админа: /admin <id>"},
            {"command": "unadmin", "description": "Снять админа: /unadmin <id>"},
            {"command": "subs", "description": "Список подписчиков"},
            {"command": "delsub", "description": "Убрать подписку: /delsub <id>"},
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
                    "-x",
                    "--audio-format", "mp3",
                    "--audio-quality", "5",
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
                mp3 = out + ".mp3"
                if rc != 0 or not os.path.exists(mp3):
                    err_text = (stderr or stdout or "yt-dlp failed").strip()
                    if ("Video unavailable" in err_text and not canon_done
                            and ("youtube.com/watch?v=" in real_url or "youtu.be/" in real_url)):
                        new_url = self._canonical_yt_url(real_url)
                        if new_url != real_url:
                            log("yt id case fix: %s -> %s" % (real_url, new_url))
                            real_url = new_url
                            canon_done = True
                            continue
                    if "Sign in to confirm" in err_text or "LOGIN_REQUIRED" in err_text:
                        self.api_q.put(("download_fail", url, title, "YouTube заблокировал это видео на нашем сервере (Sign in to confirm you're not a bot). Попробуй другой запрос или прямую ссылку — популярные видео обычно играют."))
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
                self.api_q.put(("download_ok", url, real_title, mp3))
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
                     "--rate", str(VOICE_RATE), "--channels=1"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                src = cap.stdout
            else:
                cmd = ["ffmpeg", "-y"]
                if offset_ms > 0:
                    cmd += ["-ss", "%.3f" % (offset_ms / 1000.0)]
                cmd += ["-i", path, "-vn", "-f", "s16le", "-ac", "1",
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
                ab.nChannels = 1
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
                    ab.nChannels = 1
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
        self._start_voice(path, 0)

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

    def _pause_or_resume(self):
        if self.playing and self.paused:
            self._resume()
        elif self.playing:
            self._pause()
        else:
            self._send("Сейчас ничего не играет.")

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
        self._set_status("")
        self._enqueue_url(key, label)

    def _play_search_index(self, idx, silent=False):
        self.silent = silent
        if not self.search_results:
            self._send("Список результатов пуст. Сначала поищи: п <запрос>")
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
        lines.append("пл <страница> — листать, n/b — следующий/предыдущий трек")
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
        self._set_status("")
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
            lines.append("Радио <номер> — запуск")
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
        self._send("\n".join(lines) + "\nРадио <номер> — запуск")

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
        admin_only = first in ("rs", "рестарт", "restart", "перезагрузка", "cn", "sv", "svc", "сервис", "cm", "channel")
        if not admin_only:
            admin_only = cmd.startswith("lf ") or cmd.startswith("файл ") or cmd.startswith("локальный ")
        if admin_only and not self._is_admin(from_user):
            self._send("Эта команда только для администраторов.")
            return

        # --- громкость: v 100 / v 50 / громкость 30 / volume 80 ---
        m = re.match(r"^(?:v|громкость|громко|volume)\s+(\d{1,3})$", cmd)
        if m:
            self._set_volume(int(m.group(1)))
            return

        # --- перемотка: sf 5 (вперёд на 5с), sf -5 (назад на 5с) ---
        m = re.match(r"^sf\s+(-?\d{1,6})$", cmd)
        if m:
            self._seek(int(m.group(1)))
            return

        # --- подписка на уведомления: sub / /sub (в TeamTalk — личное сообщение) ---
        if cmd == "sub" or cmd.startswith("sub "):
            self._sub_cmd()
            return

        # --- сообщения в канал: cm — вкл/выкл (по умолчанию ответы в личку) ---
        if cmd == "cm":
            self.channel_msg = not self.channel_msg
            self._save_channel_msg()
            self._send("Сообщения в канал: %s" % ("вкл ✅" if self.channel_msg else "выкл ⭕"))
            return

        # --- перезапуск бота: rs ---
        if cmd in ("rs", "рестарт", "restart", "перезагрузка"):
            self._send("🔄 Перезапускаюсь…")
            threading.Thread(target=_restart_bot_soon, daemon=True).start()
            return

        # --- выбор сервиса: sv yt / sv ym / sv ---
        first = cmd.split(None, 1)[0]
        if first in ("sv", "svc", "сервис"):
            parts = text.split(None, 1)
            if len(parts) < 2:
                cur = {"yt": "YouTube", "ym": "Яндекс.Музыка"}.get(self.service, "?")
                self._send("Сейчас: %s. Сменить: sv yt или sv ym." % cur)
                return
            svc = parts[1].strip().lower()
            if svc in ("yt", "youtube", "ютуб", "ютюб"):
                self.service = "yt"
                self._send("🎬 Сервис: YouTube.")
            elif svc in ("ym", "ya", "yandex", "яндекс", "яндекс музыка", "ямузыка"):
                self.service = "ym"
                self._send("🎵 Сервис: Яндекс.Музыка.")
            else:
                self._send("Не знаю сервис «%s». Доступно: yt (YouTube), ym (Яндекс.Музыка)." % svc)
            return

        # --- смена ника: cn <ник> ---
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
            try:
                with open(NICKNAME_FILE, "w") as f:
                    f.write(nick)
            except Exception as e:
                log("nickname save err: %s" % e)
            self.doChangeNickname(nick)
            self._send("✅ Ник: %s" % nick)
            return

        # --- стоп / скип / очередь / статус / помощь ---
        if cmd in ("с", "s", "стоп", "останови", "stop"):
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
            self._set_status("")
            self._send("⏹ Стоп.")
            return

        if cmd in ("скип", "дальше", "след", "следующий", "skip"):
            self._advance()
            return

        if cmd in ("очередь", "queue", "q"):
            self._queue_cmd()
            return

        if cmd in ("статус", "status", "now"):
            self._status_cmd()
            return

        if cmd in ("помощь", "help", "h", "команды", "commands"):
            self._help_cmd()
            return

        # --- локальный файл: lf <путь> (из Telegram-моста) ---
        if cmd.startswith("lf ") or cmd.startswith("файл ") or cmd.startswith("локальный "):
            path = text.split(None, 1)[1].strip()
            if not os.path.isfile(path):
                self._send("Файл не найден: %s" % path)
                return
            self._play_local(path, os.path.basename(path))
            return

        # --- n/b: следующий/предыдущий (по активному плейлисту или списку поиска) ---
        if cmd in ("n", "н", "next"):
            if self.auto_playlist:
                self._play_playlist_index(self.playlist_index + 1)
            elif self.auto_list:
                self._play_search_index(self.search_index + 1)
            elif self.playlist:
                self._play_playlist_index(self.playlist_index + 1)
            else:
                self._play_search_index(self.search_index + 1)
            return

        if cmd in ("b", "back", "назад"):
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
        if cmd_first in ("пл", "список", "плейлист"):
            parts = text.split(None, 1)
            page = 1
            if len(parts) > 1 and parts[1].strip().isdigit():
                page = int(parts[1].strip())
            self._send("\n".join(self._playlist_page_lines(page)))
            return

        # --- радио: радио / радио <N> / радио <текст> ---
        if cmd.startswith("радио") or cmd.startswith("radio") or cmd == "r":
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            self._radio_cmd(arg)
            return

        # --- избранное: f / f + / f + <ссылка> / f <номер> ---
        if cmd == "f" or cmd.startswith("f "):
            arg = text.split(None, 1)[1].strip() if " " in text else ""
            self._fav_cmd(arg)
            return

        # --- play: bare «пи»/«play» = продолжить, если пауза ---
        if cmd in ("пи", "pi", "play", "плей", "играй"):
            if self.paused:
                self._resume()
            elif self.playing:
                self._send("Уже играет. с — стоп, скип — дальше.")
            else:
                self._send("Что играем? п <запрос> или ссылка.")
            return

        if cmd in ("п", "p"):
            self._pause_or_resume()
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

        # --- играть: п <запрос>, пи <запрос>, play <запрос>, /play <запрос|ссылка> ---
        m = re.match(r"^(?:п|p|пи|pi|play|плей|играй|найди)\s+(\S.*)$", low)
        if m or cmd.startswith("play"):
            # аргумент берём из оригинального text (не lower), чтобы не портить регистр URL
            parts = text.split(None, 1)
            query = parts[1].strip() if len(parts) > 1 else None
            if not query:
                self._send("Дай ссылку или запрос: п <запрос>, /play <ссылка>.")
                return
            u = URL_RE.search(query)
            if u:
                self._handle_url(u.group(0), u.group(0))
            else:
                self._do_search(query)
            return

        # --- играть по прямой ссылке независимо от сервиса: u <url> / ссылка <url> / link <url> ---
        m = re.match(r"^(?:u|ссылка|link|url)\s+(\S.*)$", low)
        if m:
            parts = text.split(None, 1)
            u = URL_RE.search(parts[1]) if len(parts) > 1 else None
            if not u:
                self._send("Дай ссылку: u https://…")
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
        lines.append("Отправь h — справка по командам.")
        self._send("\n".join(lines))

    def _online_text(self):
        """Кто сейчас на сервере (nickname/username, без самого бота)."""
        users = []
        for uid, u in list(self.users.items()):
            if uid == self.my_user_id:
                continue
            nick = self._tt_field(u, "szNickname") or self._tt_field(u, "szUsername")
            if nick:
                users.append(nick)
        users = sorted(set(users))
        server = self._server_name()
        if not users:
            return "Сейчас на сервере «%s» никого, кроме меня." % server
        return "Сейчас на сервере «%s» (%d):\n%s" % (server, len(users), ", ".join(users))

    def _help_cmd(self):
        self._send(
            "п <запрос> — поиск (покажет список), играет №1\n"
            "n — следующий, b — предыдущий (по списку или плейлисту)\n"
            "пл <страница> — полный список плейлиста постранично\n"
            "пи — play, п — пауза\n"
            "с — стоп, скип — дальше (очередь)\n"
            "sf <секунды> — перемотка (sf -5 — назад)\n"
            "u <url> / ссылка <url> — играть по ссылке (независимо от сервиса)\n"
            "радио — список станций (радио <номер> — запуск)\n"
            "v <1-100> — громкость (мгновенно)\n"
            "cm — сообщения в канал вкл/выкл (по умолчанию ответы в личку)\n"
            "sv yt / sv ym — сервис\n"
            "cn <ник> — сменить ник бота\n"
            "lf <путь> — играть локальный файл\n"
            "rs — перезапустить бота\n"
            "очередь, статус, помощь\n"
            "/channel <путь> — сменить канал"
        )

    # ----- events ----------------------------------------------------
    def onConnectSuccess(self):
        log("connected, logging in")
        self._login()

    def onConnectFailed(self):
        log("connect failed")
        self._schedule_reconnect()

    def onConnectionLost(self):
        log("connection lost")
        self.playing = False
        self.logged_in = False
        self.joined = False
        self._schedule_reconnect()

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
        if START_COMMANDS:
            threading.Thread(target=self._run_startup_commands, daemon=True).start()

    def _run_startup_commands(self):
        time.sleep(3)
        for c in START_COMMANDS:
            try:
                log("startup cmd: %s" % c)
                self._handle_cmd(c)
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
        return bool(u and int(getattr(u, "uUserType", 0) or 0) == 2)

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
        self._tg_send_text(cid, "✅ Админ назначен: %s (users.db обновлён)." % aid)

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
            if sign == "+":
                text = "%s присоединился к серверу %s" % (nick, self._server_name())
            else:
                text = "%s покинул сервер %s" % (nick, self._server_name())
            if TG_NOTIFY_CHAT_ID:
                self._tg_send_notify(text, TG_NOTIFY_CHAT_ID)
            for cid, rec in list(self.sub_active.items()):
                if rec.get("username") and str(rec["username"]).lower() == uname:
                    continue  # не анонсируем подписчику его собственный вход/выход
                self._tg_send_notify(text, int(cid))
        except Exception as e:
            log("notify join/leave err: %s" % str(e)[:150])

    def onCmdUserLoggedIn(self, user):
        try:
            self.users[user.nUserID] = user
        except Exception:
            pass
        self._notify_join_leave("+", user)

    def onCmdUserLoggedOut(self, user):
        try:
            self.users.pop(user.nUserID, None)
        except Exception:
            pass
        self._notify_join_leave("-", user)

    def onCmdSuccess(self, cmdId):
        pass

    def onCmdError(self, cmdId, errmsg):
        msg = errmsg.szErrorMsg if errmsg else ""
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "ignore")
        log("cmd error (cmd %d): %s" % (cmdId, msg))
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

    def _schedule_reconnect(self):
        if not self.reconnect:
            return
        def do_reconnect():
            time.sleep(10)
            self.api_q.put(("reconnect",))
        threading.Thread(target=do_reconnect, daemon=True).start()

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
                    self._set_status("")
                    self.playlist = items
                    self._send("\n".join(self._playlist_page_lines(1)))
                    self._play_playlist_index(0)
                elif kind == "advance":
                    self._advance(silent=True)
                elif kind == "voice_finished":
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
                elif kind == "reconnect":
                    if self.reconnect and not self.connecting:
                        self.connecting = True
                        log("reconnecting")
                        self.connect(HOST, TCP_PORT, UDP_PORT)
                        self.connecting = False
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
    bot.run()


if __name__ == "__main__":
    main()
