# Speak an audio file into a TeamTalk voice channel as synthesized narration.
# Usage: tt_speak.py <ogg|mp3|wav...> [channel_id] [volume_percent]
# Defaults: channel 1 (root), volume 5%.
# Volume is applied by scaling the PCM samples (like the music bot does).
import sys
import subprocess
import time
import array
import ctypes

import TeamTalk5
from TeamTalk5 import AudioBlock, StreamType

HOST = b"ranjerserver.ru"
TCP = 10989
UDP = 10989
NICK = "агент Кирилла".encode("utf-8")
USER = b"1"
PW = b"1"
CLIENT = b"py-agent-speak"

VOICE_RATE = 48000
VOICE_CHUNK = 960  # samples per block (20 ms at 48 kHz)
VOICE_CHUNK_BYTES = VOICE_CHUNK * 2  # s16 mono


def decode_to_pcm(path):
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-f", "s16le",
           "-ac", "1", "-ar", str(VOICE_RATE), "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg decode failed: %s" % p.stderr.decode(errors="replace")[:300])
    return p.stdout


def scale_pcm(data, volume):
    if volume >= 0.999:
        return data
    a = array.array("h")
    a.frombytes(data)
    for i in range(len(a)):
        a[i] = int(a[i] * volume)
    return a.tobytes()


class Speak(TeamTalk5.TeamTalk):
    def __init__(self, path, channel_id, volume):
        super().__init__()
        self.path = path
        self.channel_id = channel_id
        self.volume = volume
        self.my_id = 0
        self.my_channel = 0
        self.done = False
        self.err = None

    def onConnectSuccess(self):
        self.doLogin(NICK, USER, PW, CLIENT)

    def onCmdMyselfLoggedIn(self, userid, useraccount):
        self.my_id = userid
        self.doJoinChannelByID(self.channel_id, b"")

    def onCmdUserJoinedChannel(self, user):
        if user.nUserID == self.my_id:
            self.my_channel = user.nChannelID
            try:
                self._stream()
            except Exception as e:
                self.err = e
            self.done = True

    def onConnectFailed(self):
        self.err = RuntimeError("connect failed")
        self.done = True

    def onConnectionLost(self):
        self.err = RuntimeError("connection lost")
        self.done = True

    def onCmdMyselfKickedFromChannel(self, channelid, user):
        self.err = RuntimeError("kicked from channel")
        self.done = True

    def _stream(self):
        pcm = decode_to_pcm(self.path)
        if not pcm:
            return
        stream_id = int(time.time() * 1000) & 0xFFFF
        buf = scale_pcm(pcm, self.volume)
        n = len(buf)
        written = 0
        block_dur = VOICE_CHUNK / float(VOICE_RATE)
        next_slot = time.monotonic()
        while written < n:
            chunk = buf[written:written + VOICE_CHUNK_BYTES]
            if len(chunk) < VOICE_CHUNK_BYTES:
                chunk += b"\x00" * (VOICE_CHUNK_BYTES - len(chunk))
            raw = (ctypes.c_char * VOICE_CHUNK_BYTES).from_buffer_copy(chunk)
            ab = AudioBlock()
            ab.nStreamID = stream_id
            ab.nSampleRate = VOICE_RATE
            ab.nChannels = 1
            ab.lpRawAudio = ctypes.cast(raw, ctypes.c_void_p)
            ab.nSamples = VOICE_CHUNK
            ab.uStreamTypes = StreamType.STREAMTYPE_VOICE
            if not self.insertAudioBlock(ab):
                raise RuntimeError("insertAudioBlock returned false")
            written += VOICE_CHUNK_BYTES
            next_slot += block_dur
            delay = next_slot - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_slot = time.monotonic()
        self._end_stream()

    def _end_stream(self):
        # End the raw-audio voice input session (lpAudioBlock=NULL).
        TeamTalk5._InsertAudioBlock(self._tt, None)


def main():
    if len(sys.argv) < 2:
        print("usage: tt_speak.py <audio> [channel_id] [volume_percent]", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    channel_id = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    volume = float(sys.argv[3]) / 100.0 if len(sys.argv) > 3 else 0.05
    s = Speak(path, channel_id, volume)
    print("connecting, channel=%d volume=%.0f%%" % (channel_id, volume * 100))
    if not s.connect(HOST, TCP, UDP):
        print("connect() returned false", file=sys.stderr)
        sys.exit(1)
    deadline = time.time() + 40
    while not s.done and time.time() < deadline:
        s.runEventLoop(100)
    if s.err:
        print("error:", s.err, file=sys.stderr)
        sys.exit(1)
    if not s.done:
        print("timeout (no channel join event)", file=sys.stderr)
        sys.exit(1)
    print("spoke into channel %d (my uid %d)" % (s.my_channel, s.my_id))
    try:
        s.doLogout()
    except Exception:
        pass
    try:
        s.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
