#!/usr/bin/env python3
"""
ITU2 emulator — PTP/IP initiator.

Speaks PTP/IP to a Canon camera on TCP 15740 as the *initiator*. Opcode set taken
from captured auto-send sessions with an EOS 250D (see PROTOCOL.md).

PTP/IP packet framing: uint32 length, uint32 type, body.
  1 InitCmdReq   2 InitCmdAck   3 InitEventReq   4 InitEventAck   5 InitFail
  6 OpReq        7 OpResp       8 Event          9 StartData     10 Data
  11 EndData     12 Cancel
"""

from __future__ import annotations

import os
import socket
import struct
import threading
import time

# ---- PTP/IP types ----
T_INIT_CMD_REQ = 1
T_INIT_CMD_ACK = 2
T_INIT_EVENT_REQ = 3
T_INIT_EVENT_ACK = 4
T_INIT_FAIL = 5
T_OP_REQ = 6
T_OP_RESP = 7
T_EVENT = 8
T_START_DATA = 9
T_DATA = 10
T_END_DATA = 11
T_CANCEL = 12

TYPE_NAME = {
    1: "InitCmdReq",
    2: "InitCmdAck",
    3: "InitEventReq",
    4: "InitEventAck",
    5: "InitFail",
    6: "OpReq",
    7: "OpResp",
    8: "Event",
    9: "StartData",
    10: "Data",
    11: "EndData",
    12: "Cancel",
}

# ---- PTP operation codes seen in the capture ----
OP_GET_DEVICE_INFO = 0x1001
OP_OPEN_SESSION = 0x1002
OP_CLOSE_SESSION = 0x1003
OP_9101 = 0x9101
OP_9102 = 0x9102
OP_9114 = 0x9114  # some remote-mode-ish toggle (param 0x41 on, 0 off)
OP_9115 = 0x9115  # set event mode
OP_9116 = 0x9116  # get event (poll)
OP_9127 = 0x9127  # enumerate (dirs/objects)
OP_913D = 0x913D
OP_913F = 0x913F
OP_916D = 0x916D
OP_9170 = 0x9170
OP_9173 = 0x9173
OP_9174 = 0x9174  # partial object read [handle, offset, length, 0, 0]
OP_9177 = 0x9177
OP_91E9 = 0x91E9
OP_91F0 = 0x91F0

RESP_OK = 0x2001
RESP_NAMES = {
    0x2001: "OK",
    0x2002: "GeneralError",
    0x2019: "DeviceBusy",
    0x201A: "InvalidParameter",
    0x201E: "SessionNotOpen",
    0x201F: "InvalidTransactionID",
    0x2005: "OperationNotSupported",
    0x2007: "ParameterNotSupported",
    0x200F: "UnknownVendorCode",
    0x2011: "DevicePropNotSupported",
    0x2013: "OperationNotSupported",
}


def _pkt(ptype: int, body: bytes = b"") -> bytes:
    return struct.pack("<II", 8 + len(body), ptype) + body


class PTPIPError(Exception):
    pass


class PTPIPClient:
    def __init__(
        self,
        ip: str,
        guid_hex: str,
        name: str,
        log=print,
        timeout: float = 10.0,
        save_dir: str | None = None,
    ):
        self.ip = ip
        self.guid = bytes.fromhex(guid_hex.replace("-", ""))
        self.name = name
        self.log = log
        self.timeout = timeout
        self.save_dir = save_dir
        self.cmd: socket.socket | None = None
        self.event: socket.socket | None = None
        self.conn_number = 0
        self.tid = 0
        self._event_thread = None
        self._stop = threading.Event()
        self.events: list[tuple[int, list[int]]] = []

    # ---------- low-level ----------
    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise PTPIPError("connection closed")
            buf += chunk
        return buf

    def _recv_pkt(self, sock: socket.socket) -> tuple[int, bytes]:
        ln, typ = struct.unpack("<II", self._recv_exact(sock, 8))
        body = self._recv_exact(sock, ln - 8) if ln > 8 else b""
        return typ, body

    def _describe(self, typ: int, body: bytes) -> str:
        try:
            if typ == T_INIT_CMD_REQ:
                g = body[:16].hex()
                nm = body[16:-4].split(b"\x00\x00")[0].decode("utf-16le", "replace")
                return f"guid={g} name={nm!r} ver={body[-4:].hex()}"
            if typ == T_INIT_CMD_ACK:
                conn = struct.unpack_from("<I", body, 0)[0]
                g = body[4:20].hex()
                nm = body[20:-4].split(b"\x00\x00")[0].decode("utf-16le", "replace")
                return f"conn={conn} guid={g} name={nm!r}"
            if typ == T_INIT_EVENT_REQ:
                return f"conn={struct.unpack_from('<I', body, 0)[0]}"
            if typ == T_INIT_FAIL:
                return f"reason={struct.unpack_from('<I', body, 0)[0]}"
            if typ == T_OP_REQ:
                dp, code, tid = struct.unpack_from("<IHI", body, 0)
                ps = [
                    struct.unpack_from("<I", body, 10 + 4 * i)[0]
                    for i in range((len(body) - 10) // 4)
                ]
                return (
                    f"op=0x{code:04X} tid={tid} dp={dp} params={[hex(p) for p in ps]}"
                )
            if typ == T_OP_RESP:
                rc, tid = struct.unpack_from("<HI", body, 0)
                ps = [
                    struct.unpack_from("<I", body, 6 + 4 * i)[0]
                    for i in range((len(body) - 6) // 4)
                ]
                return (
                    f"resp=0x{rc:04X} ({RESP_NAMES.get(rc, '?')}) tid={tid} "
                    f"params={[hex(p) for p in ps]}"
                )
            if typ == T_EVENT:
                ev, tid = struct.unpack_from("<HI", body, 0)
                ps = [
                    struct.unpack_from("<I", body, 6 + 4 * i)[0]
                    for i in range((len(body) - 6) // 4)
                ]
                return f"event=0x{ev:04X} tid={tid} params={[hex(p) for p in ps]}"
            if typ in (T_START_DATA,):
                tid = struct.unpack_from("<I", body, 0)[0]
                tl = struct.unpack_from("<Q", body, 4)[0]
                return f"tid={tid} totalLen={tl}"
            if typ in (T_DATA, T_END_DATA):
                return f"len={len(body)}"
        except Exception as e:
            return f"(parse {e})"
        return f"len={len(body)}"

    # ---------- session ----------
    def connect(self):
        self.log(f"PTP/IP connecting to {self.ip}:15740")
        self.cmd = socket.create_connection((self.ip, 15740), timeout=self.timeout)
        self.cmd.settimeout(self.timeout)
        body = (
            self.guid
            + (self.name + "\x00").encode("utf-16le")
            + struct.pack("<I", 0x00010000)
        )
        self.log(f"-> InitCmdReq guid={self.guid.hex()} name={self.name!r}")
        self.cmd.sendall(_pkt(T_INIT_CMD_REQ, body))
        typ, b = self._recv_pkt(self.cmd)
        self.log(f"<- {TYPE_NAME.get(typ, typ)} {self._describe(typ, b)}")
        if typ == T_INIT_FAIL:
            raise PTPIPError(f"camera refused connection (InitFail {b.hex()})")
        if typ != T_INIT_CMD_ACK:
            raise PTPIPError(f"unexpected {TYPE_NAME.get(typ, typ)} after InitCmdReq")
        self.conn_number = struct.unpack_from("<I", b, 0)[0]
        # event socket
        self.event = socket.create_connection((self.ip, 15740), timeout=self.timeout)
        self.event.settimeout(1.0)
        self.event.sendall(_pkt(T_INIT_EVENT_REQ, struct.pack("<I", self.conn_number)))
        typ, b = self._recv_pkt(self.event)
        self.log(f"<- (event) {TYPE_NAME.get(typ, typ)} {self._describe(typ, b)}")
        self._event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self._event_thread.start()
        return self

    def _event_loop(self):
        while not self._stop.is_set():
            try:
                typ, body = self._recv_pkt(self.event)
            except socket.timeout:
                continue
            except Exception:
                break
            self.log(f"<- EVENT {TYPE_NAME.get(typ, typ)} {self._describe(typ, body)}")
            if typ == T_EVENT:
                try:
                    ev, tid = struct.unpack_from("<HI", body, 0)
                    ps = [
                        struct.unpack_from("<I", body, 6 + 4 * i)[0]
                        for i in range((len(body) - 6) // 4)
                    ]
                    self.events.append((ev, ps))
                except Exception:
                    pass

    def op(self, code: int, params=(), dp: int = 1, expect_data: bool = False):
        self.tid += 1
        tid = self.tid
        body = struct.pack("<IHI", dp, code, tid) + b"".join(
            struct.pack("<I", p) for p in params
        )
        self.log(f"-> OpReq 0x{code:04X} tid={tid} params={[hex(p) for p in params]}")
        self.cmd.sendall(_pkt(T_OP_REQ, body))
        data = b""
        while True:
            typ, b = self._recv_pkt(self.cmd)
            self.log(f"<- {TYPE_NAME.get(typ, typ)} {self._describe(typ, b)}")
            if typ == T_START_DATA:
                total = struct.unpack_from("<Q", b, 4)[0]
                dtid = struct.unpack_from("<I", b, 0)[0]
                data = b""
                while len(data) < total:
                    t2, b2 = self._recv_pkt(self.cmd)
                    # Canon delivers data in type 10 (Data) packets and the final
                    # chunk in type 12 (its "end data"), not the spec's type 11.
                    if t2 in (T_DATA, T_END_DATA, T_CANCEL):
                        dti = struct.unpack_from("<I", b2, 0)[0]
                        if dti == dtid:
                            data += b2[4:]
                continue
            if typ == T_OP_RESP:
                rc, rtid = struct.unpack_from("<HI", b, 0)
                ps = [
                    struct.unpack_from("<I", b, 6 + 4 * i)[0]
                    for i in range((len(b) - 6) // 4)
                ]
                if data and self.save_dir:
                    try:
                        os.makedirs(self.save_dir, exist_ok=True)
                        fn = os.path.join(self.save_dir, f"op{code:04X}_tid{tid}.bin")
                        with open(fn, "wb") as f:
                            f.write(data)
                    except Exception as e:
                        self.log(f"  (save failed: {e})")
                if rtid == tid or True:
                    return rc, ps, data
            # ignore other packets (events arriving on cmd channel etc.)

    def poll_event(self):
        try:
            return self.op(OP_9116, ())
        except PTPIPError:
            return None, None, None

    def close(self):
        self._stop.set()
        for s in (self.cmd, self.event):
            try:
                if s:
                    s.close()
            except Exception:
                pass

    # ---------- high-level helpers ----------
    def open_session(self, session_id: int = 0x41):
        return self.op(OP_OPEN_SESSION, (session_id,))

    def get_device_info(self):
        rc, ps, data = self.op(OP_GET_DEVICE_INFO, (), expect_data=True)
        return rc, ps, data

    def vendor_setup(self):
        """Replay the observed session preamble."""
        results = []
        results.append(("0x9114",) + self.op(OP_9114, (0x41,)))
        results.append(("0x9115",) + self.op(OP_9115, (1,)))
        results.append(("0x913D",) + self.op(OP_913D, (4095,)))
        results.append(("0x9101",) + self.op(OP_9101, ()))
        results.append(("0x9102",) + self.op(OP_9102, (0x00020001,)))
        return results

    # exact PC->camera op sequence observed in the capture (setup portion)
    PREAMBLE = [
        (0x9114, [0x41]),
        (0x9115, [1]),
        (0x9116, []),
        (0x9170, [2442133504]),
        (0x9170, [2442133504]),
        (0x9102, [0x00020001]),
        (0x913D, [4095]),
        (0x9101, []),
        (0x9102, [0x00020001]),
        (0x1001, []),
        (0x1001, []),
        (0x9116, []),
        (0x913F, [16777216, 272, 822083584]),
        (0x9101, []),
        (0x9102, [0x00020001]),
        (0x916D, [0]),
        (0x9115, [0]),
        (0x9114, [0]),
        (0x1003, []),
    ]

    def replay_preamble(self):
        results = []
        for code, params in self.PREAMBLE:
            rc, ps, data = self.op(code, params)
            results.append((code, rc, ps, len(data)))
            if code == 0x1003:  # CloseSession — stop
                break
        return results

    # ---- session setup / pairing / transfer ----

    SETUP = [
        (0x9114, [0x41]),
        (0x9115, [1]),
        (0x9116, []),
        (0x9170, [0x91900000]),
        (0x9170, [0x91900000]),
        (0x9102, [0x00020001]),
        (0x913D, [0xFFF]),
        (0x9101, []),
        (0x9102, [0x00020001]),
    ]

    def start_session(self):
        """OpenSession + the ITU2 setup ops (no shutdown)."""
        out = [("OpenSession",) + self.open_session()]
        for code, params in self.SETUP:
            out.append((f"0x{code:04X}",) + self.op(code, params))
        return out

    def finish_pairing(self):
        """Shutdown tail used to complete a pairing session."""
        out = []
        for code, params in [(0x916D, [0]), (0x9115, [0]), (0x9114, [0]), (0x1003, [])]:
            out.append((f"0x{code:04X}",) + self.op(code, params))
        return out

    def get_event(self):
        rc, ps, data = self.op(0x9116, ())
        return data

    # ---- event / object parsing ----
    K_PROP = 0x0000C189
    K_OBJ = 0x0000C1AB

    @staticmethod
    def parse_events(data: bytes):
        out = []
        i = 0
        while i + 8 <= len(data):
            ln, kind = struct.unpack_from("<II", data, i)
            if ln < 8 or i + ln > len(data):
                break
            out.append((kind, data[i + 8 : i + ln]))
            i += ln
        return out

    @staticmethod
    def parse_objects(events):
        """Return [(handle, size, path), ...] from 0x9116 object records."""
        objs = []
        for kind, body in events:
            if kind == 0x0000C1AB:
                if len(body) < 16:
                    continue
                handle = struct.unpack_from("<I", body, 0)[0]
                size = struct.unpack_from("<I", body, 12)[0]
                rest = body[24:]
                path = rest.split(b"\x00")[0].decode("latin1", "replace")
                objs.append((handle, size, path))
        return objs

    def get_object_info(self, handle: int):
        return self.op(0x9170, (handle,))

    def read_object(self, handle: int, size: int, chunk: int = 0x200000):
        # Mirror the captured sequence: capabilities + status, read, then flush.
        try:
            self.op(0x913F, (0x01000000, 0x110, 0x31000000))
            self.op(0x9101, ())
        except Exception:
            pass
        buf = bytearray()
        offset = 0
        while offset < size:
            n = min(chunk, size - offset)
            rc, ps, data = self.op(0x9174, (handle, offset, n, 0, 0))
            try:
                self.op(0x9102, (0x00020001,))
            except Exception:
                pass
            if not data:
                break
            buf += data
            offset += len(data)
            try:
                self.op(0x91E9, (handle, min(100, int(100 * offset / max(size, 1)))))
            except Exception:
                pass
            if len(data) < n:
                break
        for code, params in (
            (0x91F0, (handle,)),
            (0x91E9, (handle, 100)),
            (0x9177, (handle, 0)),
        ):
            try:
                self.op(code, params)
            except Exception:
                pass
        return bytes(buf)
