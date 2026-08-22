#!/usr/bin/env python3
"""Diagnostic: connect, login, list channels on the TeamTalk server."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import TeamTalk5

HOST = os.environ.get("TEAMTALK_HOST", "example.com")
PORT = int(os.environ.get("TEAMTALK_TCP_PORT", "10333"))
NICK = "DiagBot"
USER = os.environ.get("TEAMTALK_USERNAME", "example")
PWD = os.environ.get("TEAMTALK_PASSWORD", "")


class Diag(TeamTalk5.TeamTalk):
    def connect(self, host, tcp, udp, ltcp=0, ludp=0, enc=False):
        return TeamTalk5._Connect(self._tt, host.encode(), tcp, udp, ltcp, ludp, enc)

    def doLogin(self, nick, user, pwd, client):
        return TeamTalk5._DoLoginEx(self._tt, nick.encode(), user.encode(), pwd.encode(), client.encode())

    def doJoinChannelByID(self, cid, pwd):
        return TeamTalk5._DoJoinChannelByID(self._tt, cid, pwd.encode())

    def getChannelIDFromPath(self, path):
        return TeamTalk5._GetChannelIDFromPath(self._tt, path.encode())

    def onConnectSuccess(self):
        print("CONNECTED")
        self.doLogin(NICK, USER, PWD, "diag")

    def onConnectFailed(self):
        print("CONNECT FAILED")

    def onConnectionLost(self):
        print("CONNECTION LOST")

    def onCmdMyselfLoggedIn(self, userid, ua):
        print("LOGGED IN, userid=%d" % userid)
        root = self.getRootChannelID()
        print("root channel id: %d" % root)
        print("path '/' -> %s" % self.getChannelIDFromPath("/"))
        try:
            chans = self.getServerChannels()
            print("channels on server: %d" % len(chans))
            for c in chans:
                name = c.szName
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "ignore").rstrip("\x00")
                print("  id=%d parent=%d name=%r" % (c.nChannelID, c.nParentID, name))
        except Exception as e:
            print("getServerChannels error: %s" % e)
        print("my user id: %d" % self.getMyUserID())
        print("issuing join root")
        self.doJoinChannelByID(root, "")

    def onCmdSuccess(self, cmdId):
        print("CMD OK %d" % cmdId)

    def onCmdError(self, cmdId, errmsg):
        print("CMD ERROR %d %r" % (cmdId, errmsg.szErrorMsg if errmsg else None))

    def onCmdUserJoinedChannel(self, user):
        print("JOINED CHANNEL EVENT user %d" % user.nUserID)

    def onCmdMyselfKickedFromChannel(self, cid, user):
        print("KICKED FROM CHANNEL")


def main():
    d = Diag()
    print("connecting...")
    d.connect(HOST, PORT, PORT)
    for _ in range(200):
        d.runEventLoop(100)
    print("done")


if __name__ == "__main__":
    main()
