#!/usr/bin/env python3
"""
ITU2 emulator — discovery + pairing + auto-send transfer.

Advertises the Computer device, and when the camera appears:
  * pairing mode  -> completes the pairing handshake
  * transfer mode -> downloads every object the camera announces

Usage:
    python3 itu2.py [--iface-ip 192.0.2.10] [--http-port 2869] [--out downloads]
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from itu2_discovery import Discovery, guess_iface_ip, get_identity, load_state  # noqa: E402
from itu2_ptpip import PTPIPClient  # noqa: E402

_lock = threading.Lock()
_last_attempt: dict[str, float] = {}
_active: set[str] = set()
RETRY_AFTER = 5.0
OUT_DIR = "downloads"


def session(ip: str, guid: str, pc_name: str, mode: str):
    log = lambda *a: print(f"[{time.strftime('%H:%M:%S')}][{ip}]", *a, flush=True)
    cli = None
    try:
        cli = PTPIPClient(ip, guid, pc_name, log=log, save_dir="recon/captures/live")
        cli.connect()
        setup = cli.start_session()
        if mode == "Pairing":
            log("completing pairing…")
            cli.finish_pairing()
            log("pairing done")
            return
        # Transfer
        os.makedirs(OUT_DIR, exist_ok=True)
        log("transfer mode: processing events + polling…")
        done: set[int] = set()

        def process(data: bytes):
            if not data:
                return
            for handle, size, path in cli.parse_objects(cli.parse_events(data)):
                if handle in done:
                    continue
                done.add(handle)
                log(f"object 0x{handle:08X} size={size} path={path!r}")
                cli.get_object_info(handle)
                buf = cli.read_object(handle, size)
                fn = os.path.join(
                    OUT_DIR, os.path.basename(path) or f"0x{handle:08X}.bin"
                )
                with open(fn, "wb") as f:
                    f.write(buf)
                log(f"saved {fn} ({len(buf)} bytes)")

        for _name, _rc, _ps, data in setup:  # includes the initial 0x9116 object table
            process(data)
        for _ in range(60):
            process(cli.get_event())
            time.sleep(2)
        log("transfer tail")
        cli.finish_pairing()
    except Exception as e:
        log(f"session error: {type(e).__name__}: {e}")
    finally:
        if cli:
            cli.close()
        with _lock:
            _active.discard(ip)


def on_camera(d: dict, guid: str, pc_name: str):
    ip = d["ip"]
    # Only connect to a real camera that is paired to *our* GUID. Another ITU2
    # host advertises the same device type/mode but has an empty X_targetId, so
    # never connect to it (and never to our own advert, filtered in discovery).
    if not d.get("paired"):
        return
    if (d.get("friendlyName") or "").lower().startswith(("winpc", "downloader")):
        return
    now = time.time()
    with _lock:
        if ip in _active or now - _last_attempt.get(ip, 0.0) < RETRY_AFTER:
            return
        _last_attempt[ip] = now
        _active.add(ip)
    threading.Thread(
        target=session, args=(ip, guid, pc_name, d.get("mode", "?")), daemon=True
    ).start()


def main():
    global OUT_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface-ip", default=None)
    ap.add_argument("--http-port", type=int, default=2869)
    ap.add_argument("--out", default="downloads")
    ap.add_argument("--camera-ip", default=None, help="optional known camera IP probe")
    args = ap.parse_args()
    OUT_DIR = args.out
    state = load_state()
    guid, pc_name = get_identity(state)
    iface_ip = args.iface_ip or guess_iface_ip()
    print(f"identity: guid={guid} pc_name={pc_name} iface_ip={iface_ip} out={OUT_DIR}")
    disc = Discovery(iface_ip, guid, pc_name, args.http_port)
    disc.camera_ip = args.camera_ip
    disc.on_camera = lambda d: on_camera(d, guid, pc_name)
    disc.run()


if __name__ == "__main__":
    main()
