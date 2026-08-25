# Upload a local file into a TeamTalk channel (channel file storage).
# Usage: tt_upload.py <local_file> [channel_id]
import sys
import time
import ctypes

import TeamTalk5
from TeamTalk5 import ttstr, ClientEvent, FileTransferStatus

HOST = b"ranjerserver.ru"
TCP = 10989
UDP = 10989
NICK = "агент Кирилла".encode("utf-8")
USER = b"1"
PW = b"1"
CLIENT = b"py-agent-upload"


class Upload(TeamTalk5.TeamTalk):
    def __init__(self, path, channel_id):
        super().__init__()
        self.path = path.encode("utf-8") if isinstance(path, str) else path
        self.channel_id = channel_id
        self.my_id = 0
        self.my_channel = 0
        self.transfer_id = 0
        self.result = None  # ("ok"|"error", msg)

    def onConnectSuccess(self):
        self.doLogin(NICK, USER, PW, CLIENT)

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_id = userid
        print("logged in, my uid=%d, joining channel %d" % (userid, self.channel_id), flush=True)
        self.doJoinChannelByID(self.channel_id, b"")

    def onCmdUserJoinedChannel(self, user):
        if user.nUserID == self.my_id:
            self.my_channel = user.nChannelID
            print("joined channel %d, sending file" % self.my_channel, flush=True)
            time.sleep(0.2)
            self.transfer_id = self.doSendFile(self.channel_id, self.path)
            print("doSendFile -> transfer_id=%d" % self.transfer_id, flush=True)
            if self.transfer_id <= 0:
                self.result = ("error", "doSendFile returned %s" % self.transfer_id)


def main():
    if len(sys.argv) < 2:
        print("usage: tt_upload.py <local_file> [channel_id]", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    channel_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    u = Upload(path, channel_id)
    print("connecting, channel=%d file=%s" % (channel_id, path))
    if not u.connect(HOST, TCP, UDP):
        print("connect() returned false", file=sys.stderr)
        sys.exit(1)
    deadline = time.time() + 90
    done = False
    last_status = None
    while not done and time.time() < deadline:
        msg = u.getMessage(200)
        ev = msg.nClientEvent
        if ev == ClientEvent.CLIENTEVENT_CON_SUCCESS:
            u.onConnectSuccess()
        elif ev == ClientEvent.CLIENTEVENT_CON_FAILED:
            u.result = ("error", "connect failed")
            done = True
        elif ev == ClientEvent.CLIENTEVENT_CMD_MYSELF_LOGGEDIN:
            u.onCmdMyselfLoggedIn(msg.nSource, msg.useraccount)
        elif ev == ClientEvent.CLIENTEVENT_CMD_USER_JOINED:
            u.onCmdUserJoinedChannel(msg.user)
        elif ev == ClientEvent.CLIENTEVENT_FILETRANSFER:
            ft = msg.filetransfer
            st = ft.nStatus
            try:
                name = ttstr(ft.szRemoteFileName)
            except Exception:
                name = "?"
            last_status = st
            print("transfer: status=%s name=%r transferred=%d/%d" % (
                st, name, ft.nTransferred, ft.nFileSize), flush=True)
            if st == FileTransferStatus.FILETRANSFER_FINISHED:
                u.result = ("ok", "uploaded %r to channel %d" % (name, ft.nChannelID))
                done = True
            elif st in (FileTransferStatus.FILETRANSFER_ERROR,
                        FileTransferStatus.FILETRANSFER_CLOSED):
                u.result = ("error", "transfer status=%s" % st)
                done = True
        elif ev == ClientEvent.CLIENTEVENT_CMD_FILE_NEW:
            rf = msg.remotefile
            try:
                fname = ttstr(rf.szFileName)
            except Exception:
                fname = "?"
            print("file_new in channel %d: %r (%d bytes) by %s" % (
                rf.nChannelID, fname, rf.nFileSize,
                ttstr(rf.szUsername) if rf.szUsername else "?"), flush=True)
        elif ev == ClientEvent.CLIENTEVENT_CMD_ERROR:
            try:
                emsg = msg.clienterrormsg.szErrorMsg
                if isinstance(emsg, bytes):
                    emsg = emsg.decode("utf-8", "ignore")
            except Exception:
                emsg = "?"
            print("cmd_error (source=%s): %s" % (msg.nSource, emsg), flush=True)
            u.result = ("error", "cmd error: %s" % emsg)
            done = True
        elif ev != 0:
            print("event %d" % ev, flush=True)
        if u.result:
            done = True
    if not done:
        u.result = ("timeout", "no transfer event within timeout")
    try:
        u.doLogout()
    except Exception:
        pass
    try:
        u.disconnect()
    except Exception:
        pass
    print("RESULT:", u.result[0], "|", u.result[1])


if __name__ == "__main__":
    main()
