#!/usr/bin/env python3
"""Test helper: connect, join channel, send a command as a normal user."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctypes import byref

import TeamTalk5
from TeamTalk5 import TextMsgType, buildTextMessage

HOST = os.environ.get("TEAMTALK_HOST", "example.com")
PORT = int(os.environ.get("TEAMTALK_TCP_PORT", "10333"))
USER = os.environ.get("TEAMTALK_USERNAME", "example")
PWD = os.environ.get("TEAMTALK_PASSWORD", "")

CMD = sys.argv[1] if len(sys.argv) > 1 else "/status"
CHAN = int(sys.argv[2]) if len(sys.argv) > 2 else 1
LISTEN = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0


class Sender(TeamTalk5.TeamTalk):
    def connect(self, host, tcp, udp, ltcp=0, ludp=0, enc=False):
        return TeamTalk5._Connect(self._tt, host.encode(), tcp, udp, ltcp, ludp, enc)

    def doLogin(self, nick, user, pwd, client):
        return TeamTalk5._DoLoginEx(self._tt, nick.encode(), user.encode(), pwd.encode(), client.encode())

    def doJoinChannelByID(self, cid, pwd):
        return TeamTalk5._DoJoinChannelByID(self._tt, cid, pwd.encode())

    def doTextMessage(self, msg):
        return TeamTalk5._DoTextMessage(self._tt, byref(msg))

    def onConnectSuccess(self):
        self.doLogin("CmdSender", USER, PWD, "cmd-sender")

    def onCmdMyselfLoggedIn(self, userid, ua):
        self.doJoinChannelByID(CHAN, "")

    def onCmdUserJoinedChannel(self, user):
        if user.nUserID == self.getMyUserID():
            print("joined channel", file=sys.stderr)
            time.sleep(0.5)
            msgs = buildTextMessage(CMD, TextMsgType.MSGTYPE_CHANNEL, nChannelID=CHAN)
            for m in msgs:
                self.doTextMessage(m)
            print("sent: %s" % CMD, file=sys.stderr)
            if LISTEN:
                deadline = time.time() + LISTEN
                while time.time() < deadline:
                    self.runEventLoop(500)
            sys.exit(0)

    def onCmdUserTextMessage(self, tm):
        if not tm:
            return
        msg = tm.szMessage
        if isinstance(msg, bytes):
            msg = msg.decode("utf-8", "ignore").replace("\x00", "").strip()
        if not msg:
            return
        print("RECV from %d: %s" % (tm.nFromUserID, msg), flush=True)

    def onCmdError(self, cmdId, errmsg):
        print("CMD ERROR %d %r" % (cmdId, errmsg.szErrorMsg if errmsg else None), file=sys.stderr)

    def onConnectFailed(self):
        print("CONNECT FAILED", file=sys.stderr)
        sys.exit(1)


def main():
    s = Sender()
    print("connecting...", file=sys.stderr)
    s.connect(HOST, PORT, PORT)
    for _ in range(200):
        s.runEventLoop(100)
    print("timeout", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
