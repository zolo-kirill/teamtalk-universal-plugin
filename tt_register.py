# -*- coding: utf-8 -*-
"""Регистратор учётных записей TeamTalk через Telegram.

Модуль Universal Plugin: запускается из bot.py (см. start()). Отдельный
Telegram-бот принимает заявки на регистрацию (логин + пароль). Администратор
получает уведомление с кнопками «Принять»/«Отклонить». При принятии бот
создаёт учётную запись на сервере TeamTalk (под учёткой из
telegram_registration.admin_username / admin_password) и шлёт сетевое
сообщение «Пользователь X зарегистрирован на сервере».

Заявки хранятся в register_requests.json (рядом с модулем). Пароль удаляется
из файла, как только заявка обработана (принята или отклонена).
"""
import json
import os
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from TeamTalk5 import (
    TeamTalk,
    UserAccount,
    UserType,
    TextMsgType,
    buildTextMessage,
)

USERNAME_RE = re.compile(r"^[\w.\-]{3,32}$", re.UNICODE)
CREATE_TIMEOUT_SEC = 10


def _ttstr(value):
    """Байты из поля структуры SDK -> строка."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


class _Tt(TeamTalk):
    """TT-соединение регистратора (отдельная учётка-администратор)."""

    def __init__(self, reg):
        super().__init__()
        self.reg = reg

    def onConnectSuccess(self):
        self.reg.connected = True
        c = self.reg.cfg
        self.reg.log("регистратор: подключён к %s, логинюсь" % c["hostname"])
        self.doLogin(
            str(c["tt_nickname"]).encode("utf-8"),
            str(c["tt_username"]).encode("utf-8"),
            str(c["tt_password"]).encode("utf-8"),
            b"teamtalk-universal-plugin",
        )

    def onConnectFailed(self):
        self.reg.connected = False
        self.reg.log("регистратор: не удалось подключиться к TeamTalk")

    def onConnectionLost(self):
        self.reg.connected = False
        self.reg.logged_in = False
        self.reg.log("регистратор: соединение с TeamTalk потеряно")

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.reg.logged_in = True
        name = _ttstr(useraccount.szUsername)
        self.reg.log("регистратор: вошёл как %s (id %d)" % (name, userid))

    def onUserAccountNew(self, useraccount):
        name = _ttstr(useraccount.szUsername)
        self.reg.log("регистратор: создана учётка %s" % name)
        self.reg.on_account_created(name)

    def onCmdError(self, cmdId, errmsg):
        msg = getattr(errmsg, "szErrorMsg", "") or ""
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "replace")
        self.reg.on_cmd_error(cmdId, str(msg))


class Registrar(object):
    def __init__(self, cfg, log_fn=print):
        self.cfg = cfg
        self.log = log_fn
        self.token = cfg.get("token", "")
        self.admin_id = int(cfg.get("admin_user_id", 0) or 0)
        self.state_file = cfg["state_file"]
        self.state = self._load_state()
        self.offset = int(self.state.get("tg_offset", 0) or 0)
        self.tg_q = queue.Queue()
        self.stop_evt = threading.Event()
        # состояние TT (меняется только в core-потоке)
        self.tt = None
        self.connected = False
        self.logged_in = False
        self.awaiting = {}          # cmdId -> req_id
        self.pending_users = {}     # username.lower() -> req_id
        self.create_deadlines = {}  # req_id -> monotonic deadline
        self.admin_ctx = {}         # req_id -> (chat_id, message_id) — где кнопки админа
        self.conv = {}              # tg_user_id -> {"step": ..., "username": ...}
        self._core = threading.Thread(target=self._core_loop, name="reg-core", daemon=True)
        self._poller = threading.Thread(target=self._poller_loop, name="reg-tg", daemon=True)

    # ------------------------------------------------------------- lifecycle
    def start(self):
        self._poller.start()
        self._core.start()
        self.log("регистратор: запущен (token %s…, admin %s)" % (self.token[:6], self.admin_id))

    def stop(self):
        self.stop_evt.set()

    # ---------------------------------------------------------------- state
    def _load_state(self):
        try:
            with open(self.state_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"requests": {}, "history": []}

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log("регистратор: не сохранить состояние: %s" % str(e)[:200])

    # ----------------------------------------------------------- telegram api
    def _tg(self, method, **params):
        url = "https://api.telegram.org/bot%s/%s" % (self.token, method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=70) as r:
            return json.loads(r.read().decode())

    def _tg_send(self, chat_id, text, reply_markup=None):
        params = {"chat_id": chat_id, "text": text, "parse_mode": ""}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        try:
            return self._tg("sendMessage", **params)
        except Exception as e:
            self.log("регистратор: send err: %s" % str(e)[:200])
            return None

    def _tg_edit(self, chat_id, message_id, text, reply_markup=None):
        params = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": ""}
        if reply_markup is not None:
            params["reply_markup"] = json.dumps(reply_markup)
        try:
            return self._tg("editMessageText", **params)
        except Exception as e:
            self.log("регистратор: edit err: %s" % str(e)[:200])
            return None

    def _tg_answer(self, callback_id, text=""):
        try:
            self._tg("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except Exception as e:
            self.log("регистратор: answer err: %s" % str(e)[:200])

    # ------------------------------------------------------------- telegram poll
    def _poller_loop(self):
        while not self.stop_evt.is_set():
            try:
                res = self._tg("getUpdates", timeout=25, offset=self.offset)
                for upd in res.get("result", []):
                    uid = upd.get("update_id", 0) + 1
                    if uid > self.offset:
                        self.offset = uid
                        self.state["tg_offset"] = self.offset
                    self.tg_q.put(upd)
            except Exception as e:
                self.log("регистратор: poll err: %s" % str(e)[:200])
                time.sleep(5)

    # -------------------------------------------------------------- TT events
    def _ensure_tt(self):
        if self.tt is not None:
            return
        c = self.cfg
        try:
            tt = _Tt(self)
            if tt.connect(str(c["hostname"]).encode("utf-8"),
                          int(c["tcp_port"]), int(c["udp_port"])):
                self.tt = tt
                self.log("регистратор: соединяюсь с %s:%s" % (c["hostname"], c["tcp_port"]))
            else:
                tt.closeTeamTalk()
                self.tt = None
        except Exception as e:
            self.log("регистратор: connect err: %s" % str(e)[:200])
            self._drop_tt()

    def _drop_tt(self):
        if self.tt is not None:
            try:
                self.tt.closeTeamTalk()
            except Exception:
                pass
        self.tt = None
        self.connected = False
        self.logged_in = False
        self.awaiting = {}
        self.pending_users = {}
        self.create_deadlines = {}

    def _core_loop(self):
        self._ensure_tt()
        while not self.stop_evt.is_set():
            if self.tt:
                try:
                    self.tt.runEventLoop(50)
                except Exception as e:
                    self.log("регистратор: tt loop err: %s" % str(e)[:200])
                    self._drop_tt()
            self._drain_tg()
            self._check_timeouts()
            if not self.tt:
                time.sleep(2)
                self._ensure_tt()
        self._drop_tt()

    # ------------------------------------------------------------ main actions
    def _drain_tg(self):
        while True:
            try:
                upd = self.tg_q.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_update(upd)
            except Exception as e:
                self.log("регистратор: upd err: %s" % str(e)[:200])

    def _handle_update(self, upd):
        if "callback_query" in upd:
            self._handle_callback(upd["callback_query"])
            return
        msg = upd.get("message") or {}
        if not msg:
            return
        chat_id = msg.get("chat", {}).get("id")
        user = msg.get("from", {})
        user_id = user.get("id")
        text = (msg.get("text") or "").strip()
        if not text:
            return
        if text == "/start" and user_id == self.admin_id:
            self._tg_send(chat_id, "Ты администратор регистратора. Сюда будут приходить заявки на "
                                   "регистрацию с кнопками «Принять» и «Отклонить». Пользователи подают "
                                   "заявки через этого бота командой /register.")
            return
        if text in ("/start", "/register"):
            self._start_register(chat_id, user_id, user)
            return
        step = self.conv.get(user_id, {}).get("step")
        if step == "await_username":
            self._on_username(chat_id, user_id, text)
        elif step == "await_password":
            self._on_password(chat_id, user_id, text, user)
        else:
            self._tg_send(chat_id, "Команда /register — подать заявку на регистрацию учётной записи на сервере TeamTalk.")

    # ------------------------------------------------------------ registration
    def _start_register(self, chat_id, user_id, user):
        if user_id == self.admin_id:
            self._tg_send(chat_id, "Ты администратор — тебе не нужно регистрироваться. Заявки приходят сюда с кнопками «Принять» и «Отклонить».")
            return
        if self._pending_for_user(user_id):
            self._tg_send(chat_id, "У тебя уже есть заявка на проверке. Жди решения администратора.")
            return
        self.conv[user_id] = {"step": "await_username"}
        self._tg_send(chat_id, "Регистрация учётной записи на TeamTalk.\n"
                               "Придумай логин: буквы, цифры, точка, дефис или подчёркивание, "
                               "от 3 до 32 символов, без пробелов. Пришли его мне.")

    def _on_username(self, chat_id, user_id, text):
        name = text.strip()
        if not USERNAME_RE.match(name):
            self._tg_send(chat_id, "Логин не подходит. Нужны буквы, цифры, точка, дефис или "
                                   "подчёркивание, от 3 до 32 символов, без пробелов. Попробуй ещё раз.")
            return
        if self._username_pending(name):
            self._tg_send(chat_id, "Этот логин уже занят другой заявкой. Выбери другой.")
            return
        self.conv[user_id] = {"step": "await_password", "username": name}
        self._tg_send(chat_id, "Логин «%s» принят.\n"
                               "Теперь придумай пароль: минимум 4 символа, без пробелов. Пришли его." % name)

    def _on_password(self, chat_id, user_id, text, user):
        pwd = text.strip()
        if len(pwd) < 4 or any(ch.isspace() for ch in pwd):
            self._tg_send(chat_id, "Пароль должен быть не короче 4 символов и без пробелов. Попробуй ещё раз.")
            return
        conv = self.conv.get(user_id)
        if not conv or conv.get("step") != "await_password":
            self._tg_send(chat_id, "Начни с команды /register.")
            return
        username = conv["username"]
        del self.conv[user_id]
        req_id = uuid.uuid4().hex[:12]
        self.state["requests"][req_id] = {
            "username": username,
            "password": pwd,
            "tg_user_id": user_id,
            "tg_name": user.get("first_name") or user.get("username") or str(user_id),
            "tg_username": user.get("username", ""),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "pending",
        }
        self._save_state()
        self._tg_send(chat_id, "Заявка отправлена на проверку администратору. "
                               "Когда её одобрят, учётная запись будет создана.")
        self._notify_admin_new(req_id)

    def _notify_admin_new(self, req_id):
        if not self.admin_id:
            return
        req = self.state["requests"].get(req_id)
        if not req:
            return
        kb = self._kb(req_id)
        tg = ("@%s" % req["tg_username"]) if req.get("tg_username") else ""
        text = ("Новая заявка на регистрацию\n"
                "Логин: %s\n"
                "Telegram: %s %s\n"
                "Что делаем?" % (req["username"], req["tg_name"], tg)).rstrip()
        self._tg_send(self.admin_id, text, reply_markup=kb)

    @staticmethod
    def _kb(req_id):
        return {"inline_keyboard": [[
            {"text": "Принять", "callback_data": "accept:%s" % req_id},
            {"text": "Отклонить", "callback_data": "reject:%s" % req_id},
        ]]}

    # -------------------------------------------------------------- callbacks
    def _handle_callback(self, cb):
        user_id = cb.get("from", {}).get("id")
        if user_id != self.admin_id:
            self._tg_answer(cb.get("id"), "Эта кнопка не для тебя.")
            return
        data = cb.get("data", "")
        if ":" not in data:
            self._tg_answer(cb.get("id"))
            return
        action, req_id = data.split(":", 1)
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        message_id = cb.get("message", {}).get("message_id")
        req = self.state["requests"].get(req_id)
        if not req:
            self._tg_answer(cb.get("id"), "Заявка не найдена.")
            return
        if req.get("status") != "pending":
            self._tg_answer(cb.get("id"), "Заявка уже обработана.")
            return
        if action == "accept":
            self._accept(req_id, req, chat_id, message_id)
        elif action == "reject":
            self._reject(req_id, req, chat_id, message_id)
        self._tg_answer(cb.get("id"))

    def _accept(self, req_id, req, chat_id, message_id):
        if req.get("processing"):
            self._tg_edit(chat_id, message_id, "Заявка на «%s» уже в обработке." % req["username"])
            return
        if not self.logged_in:
            self._tg_edit(chat_id, message_id,
                          "Бот сейчас не подключён к серверу TeamTalk. "
                          "Нажми «Принять» ещё раз через минуту.",
                          self._kb(req_id))
            return
        req["processing"] = True
        self.admin_ctx[req_id] = (chat_id, message_id)
        self._save_state()
        self._tg_edit(chat_id, message_id, "Создаю учётную запись «%s»..." % req["username"])
        self._issue_create(req_id, req)

    def _issue_create(self, req_id, req):
        ua = UserAccount()
        ua.szUsername = req["username"].encode("utf-8")
        ua.szPassword = req["password"].encode("utf-8")
        ua.uUserType = UserType.USERTYPE_DEFAULT
        ua.uUserRights = 0
        ua.szInitChannel = b"/"
        try:
            cmd = self.tt.doNewUserAccount(ua)
        except Exception as e:
            self.log("регистратор: doNewUserAccount err: %s" % str(e)[:200])
            self._finalize_create(req_id, req, ok=False, err="ошибка вызова")
            return
        self.awaiting[cmd] = req_id
        self.pending_users[req["username"].lower()] = req_id
        self.create_deadlines[req_id] = time.monotonic() + CREATE_TIMEOUT_SEC

    def _reject(self, req_id, req, chat_id, message_id):
        req["status"] = "rejected"
        self._save_state()
        self._tg_edit(chat_id, message_id, "Заявка на «%s» отклонена." % req["username"])
        if req.get("tg_user_id"):
            self._tg_send(req["tg_user_id"], "Заявка на логин «%s» отклонена администратором." % req["username"])
        self._archive(req_id, req, approved=False)

    # ------------------------------------------------------------ async finish
    def on_account_created(self, username):
        req_id = self.pending_users.pop(username.lower(), None)
        if not req_id:
            self.log("регистратор: учётка %s создана (не по нашей заявке)" % username)
            return
        req = self.state["requests"].get(req_id)
        if not req:
            return
        self._finalize_create(req_id, req, ok=True, err="")

    def on_cmd_error(self, cmdId, msg):
        req_id = self.awaiting.pop(cmdId, None)
        if not req_id:
            return
        req = self.state["requests"].get(req_id)
        if not req:
            return
        self._finalize_create(req_id, req, ok=False, err=msg or "неизвестная ошибка")

    def _check_timeouts(self):
        now = time.monotonic()
        for req_id in list(self.create_deadlines):
            deadline = self.create_deadlines[req_id]
            if now < deadline:
                continue
            req = self.state["requests"].get(req_id)
            if req and req.get("status") == "pending" and req.get("processing"):
                self._finalize_create(req_id, req, ok=False, err="таймаут создания")
            else:
                self.create_deadlines.pop(req_id, None)
                self.pending_users.pop(req.get("username", "").lower(), None) if req else None

    def _finalize_create(self, req_id, req, ok, err):
        self.awaiting = {k: v for k, v in self.awaiting.items() if v != req_id}
        self.pending_users.pop(req["username"].lower(), None)
        self.create_deadlines.pop(req_id, None)
        admin = self.admin_ctx.pop(req_id, None)
        if req.get("status") != "pending":
            return
        if ok:
            req["status"] = "approved"
            self._save_state()
            self._broadcast_approved(req["username"])
            if admin:
                self._tg_edit(admin[0], admin[1], "Учётная запись «%s» создана на сервере." % req["username"])
            if req.get("tg_user_id"):
                self._tg_send(req["tg_user_id"],
                              "Готово! Твоя учётная запись создана:\n"
                              "Логин: %s\n"
                              "Сервер: %s:%s\n"
                              "Можно подключаться к TeamTalk."
                              % (req["username"], self.cfg["hostname"], self.cfg["tcp_port"]))
            self._archive(req_id, req, approved=True)
        else:
            req.pop("processing", None)
            self._save_state()
            if admin:
                self._tg_edit(admin[0], admin[1],
                              "Не удалось создать учётную запись «%s»: %s"
                              % (req["username"], err),
                              self._kb(req_id))
            self.log("регистратор: не удалось создать %s: %s" % (req["username"], err))

    def _broadcast_approved(self, username):
        if not self.logged_in or not self.tt:
            self.log("регистратор: нет соединения — сетевое сообщение не отправлено")
            return
        template = self.cfg.get("broadcast_text") or "Пользователь {username} зарегистрирован на сервере"
        text = template.replace("{username}", username)
        try:
            msgs = buildTextMessage(text, TextMsgType.MSGTYPE_BROADCAST, 0)
            for m in msgs:
                self.tt.doTextMessage(m)
            self.log("регистратор: сетевое сообщение: %s" % text)
        except Exception as e:
            self.log("регистратор: broadcast err: %s" % str(e)[:200])

    # ---------------------------------------------------------------- helpers
    def _archive(self, req_id, req, approved):
        self.state["requests"].pop(req_id, None)
        self.state["history"].append({
            "username": req.get("username"),
            "tg_user_id": req.get("tg_user_id"),
            "tg_name": req.get("tg_name"),
            "created_at": req.get("created_at"),
            "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "status": "approved" if approved else "rejected",
        })
        self._save_state()

    def _pending_for_user(self, tg_user_id):
        for req in self.state["requests"].values():
            if req.get("tg_user_id") == tg_user_id and req.get("status") == "pending":
                return req
        return None

    def _username_pending(self, username):
        low = username.lower()
        for req in self.state["requests"].values():
            if req.get("username", "").lower() == low and req.get("status") == "pending":
                return True
        return False


def start(cfg, log_fn=print):
    """Запускает регистратор. cfg — dict с ключами token, admin_user_id,
    hostname, tcp_port, udp_port, tt_username, tt_password, tt_nickname,
    broadcast_text, state_file. Возвращает Registrar или None."""
    if not cfg or not cfg.get("token") or not cfg.get("admin_user_id"):
        return None
    reg = Registrar(cfg, log_fn)
    reg.start()
    return reg
