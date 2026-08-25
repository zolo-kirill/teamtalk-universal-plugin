# Probe ranjerserver.ru TeamTalk: channels + users (read-only, no voice).
import time
import TeamTalk5
from TeamTalk5 import ttstr

HOST = b"ranjerserver.ru"
TCP = 10989
UDP = 10989
NICK = b"\xd0\x9f\xd1\x80\xd0\xbe\xd0\xb2\xd0\xb5\xd1\x80\xd0\xba\xd0\xb0"  # "Проверка"
USER = b"1"
PW = b"1"
CLIENT = b"py-probe"


class Probe(TeamTalk5.TeamTalk):
    def __init__(self):
        super().__init__()
        self.done = False
        self.my_id = 0

    def onConnectSuccess(self):
        self.doLogin(NICK, USER, PW, CLIENT)

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_id = userid
        time.sleep(1.2)
        try:
            root = self.getRootChannelID()
            print("root channel id:", root)
            chans = self.getServerChannels()
            print("--- channels (%d) ---" % len(chans))
            for ch in chans:
                cid = ch.nChannelID
                name = ttstr(ch.szName)
                path = self.getChannelPath(cid)
                print("  cid=%s name=%r path=%r nUsers=%s max=%s" % (
                    cid, name, ttstr(path), getattr(ch, "nUsers", "?"), ch.nMaxUsers))
            users = self.getServerUsers()
            print("--- users (%d) ---" % len(users))
            for u in users:
                try:
                    nick = ttstr(u.szNickname)
                except Exception:
                    nick = "?"
                try:
                    uname = ttstr(u.szUsername)
                except Exception:
                    uname = "?"
                print("  uid=%s nick=%r user=%r ch=%s status=%s" % (
                    u.nUserID, nick, uname, u.nChannelID, u.nStatusMode))
        except Exception as e:
            print("probe error:", type(e).__name__, e)
        self.done = True

    def onConnectFailed(self):
        print("connect FAILED")
        self.done = True

    def onConnectionLost(self):
        print("connection LOST")
        self.done = True


def main():
    p = Probe()
    print("connecting...")
    if not p.connect(HOST, TCP, UDP):
        print("connect() returned false")
        return
    deadline = time.time() + 15
    while not p.done and time.time() < deadline:
        p.runEventLoop(100)
    try:
        p.doLogout()
    except Exception:
        pass
    try:
        p.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
