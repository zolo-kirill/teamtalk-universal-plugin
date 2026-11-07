#!/usr/bin/env python3
"""Патч bot.py: чтение апдейтов из журнала tg-bridge вместо getUpdates.

Ставит три вещи:
  1) константу TG_BRIDGE_FILE (из config.json -> telegram.bridge_file);
  2) ранний выход _tg_poll в файловый режим;
  3) методы _tg_update_mine и _tg_poll_bridge.

Повторный запуск безопасен: если TG_BRIDGE_FILE уже есть в файле, ничего не меняется.
"""
import sys

POLL_OLD = '''    def _tg_poll(self):
        while True:'''

POLL_NEW = '''    def _tg_poll(self):
        if TG_BRIDGE_FILE:
            return self._tg_poll_bridge()
        while True:'''

TAIL_OLD = '''            except Exception as e:
                log("tg poll err: %s" % str(e)[:150])
                _tg_reset_session()
                time.sleep(1)'''

BRIDGE = '''
    def _tg_update_mine(self, upd):
        """Апдейт адресован этой копии: от владельца или её админа."""
        body = (upd.get("callback_query") or upd.get("message")
                or upd.get("channel_post") or {})
        from_id = (body.get("from") or {}).get("id")
        chat = ((body.get("message") or {}).get("chat") or body.get("chat") or {})
        chat_id = chat.get("id")
        ids = self._tg_admin_ids()
        if not ids:
            return False
        return from_id in ids or chat_id in ids

    def _tg_poll_bridge(self):
        """Читаю общий журнал апдейтов, который пишет tg-bridge.

        getUpdates держит один процесс на токен: иначе Telegram отвечает
        409 Conflict и отдаёт апдейт случайной копии, а владельцы у копий
        разные. Здесь каждая копия читает журнал со своей позиции, update_id
        только растёт, поэтому ни дублей, ни потерь.
        """
        path = TG_BRIDGE_FILE
        log("tg bridge: журнал %s" % path)
        fh = None
        ino = None
        primed = False
        while True:
            try:
                if not os.path.exists(path):
                    if fh:
                        fh.close()
                        fh = None
                        ino = None
                    time.sleep(1)
                    continue
                cur = os.stat(path).st_ino
                if fh is None or cur != ino:
                    if fh:
                        fh.close()
                    fh = open(path, "r", encoding="utf-8", errors="replace")
                    ino = cur
                    if not primed:
                        # при первом чтении накопленное пропускаем, чтобы
                        # после перезапуска не переигрывать старые команды
                        last = 0
                        for line in fh:
                            try:
                                last = max(last, int(json.loads(line).get("update_id") or 0))
                            except Exception:
                                pass
                        if last:
                            self._tg_offset = max(self._tg_offset, last)
                        primed = True
                        continue
                line = fh.readline()
                if not line:
                    time.sleep(0.4)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    upd = json.loads(line)
                except Exception:
                    continue
                uid = int(upd.get("update_id") or 0)
                if uid <= self._tg_offset:
                    continue
                self._tg_offset = uid
                if not self._tg_update_mine(upd):
                    continue
                self._tg_handle_update(upd)
            except Exception as e:
                log("tg bridge err: %s" % str(e)[:150])
                time.sleep(1)'''


def patch(path):
    src = open(path, encoding="utf-8").read()
    if "TG_BRIDGE_FILE" in src:
        print("%s: уже пропатчен" % path)
        return 0
    for part in (POLL_OLD, TAIL_OLD):
        if part not in src:
            print("%s: не нашёл якорь, пропускаю" % path)
            return 1
    anchor = 'TG_SEND_ONLY = str(_cfg("telegram.send_only"'
    if anchor not in src:
        print("%s: нет строки TG_SEND_ONLY" % path)
        return 1
    i = src.index(anchor)
    j = src.index("\n", i)
    const = ('\n# общий журнал апдейтов от tg-bridge: несколько копий на один токен\n'
             'TG_BRIDGE_FILE = str(_cfg("telegram.bridge_file", "TG_BRIDGE_FILE", "")).strip()')
    src = src[:j] + const + src[j:]
    src = src.replace(POLL_OLD, POLL_NEW, 1)
    src = src.replace(TAIL_OLD, TAIL_OLD + BRIDGE, 1)
    open(path, "w", encoding="utf-8").write(src)
    print("%s: пропатчен" % path)
    return 0


if __name__ == "__main__":
    rc = 0
    for p in sys.argv[1:]:
        rc |= patch(p)
    sys.exit(rc)
