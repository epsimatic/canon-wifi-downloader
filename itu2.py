#!/usr/bin/env python3
"""
ITU2 emulator — discovery + pairing + auto-send transfer.

Advertises the Computer device, and when the camera appears:
  * pairing mode  -> completes the pairing handshake
  * transfer mode -> downloads every object the camera announces

Usage:
    python3 itu2.py [--iface-ip 192.0.2.10] [--http-port 2869] [--out downloads]
                    [--flat]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import posixpath
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from itu2_discovery import (
    Discovery,
    guess_iface_ip,
    get_identity,
    load_state,
)  # noqa: E402
from itu2_history import DownloadHistory, ObjectMeta  # noqa: E402
from itu2_ptpip import PTPIPClient  # noqa: E402

_lock = threading.Lock()
_output_lock = threading.Lock()
_last_attempt: dict[str, float] = {}
_active: set[str] = set()
RETRY_AFTER = 5.0
OUT_DIR = "downloads"
HISTORY: DownloadHistory | None = None
REDOWNLOAD_MISSING = False
FLAT = False


def _safe_component(name: str, fallback: str) -> str:
    """Make one path component safe on Linux/macOS and Windows."""
    name = (name or "").strip()
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', "_", name)
    name = name.strip(" .")
    if name in ("", ".", ".."):
        return fallback
    return name


def _output_path(camera_name: str, camera_path: str, handle: int) -> str:
    """Build the destination path for one object."""
    filename = posixpath.basename(camera_path.rstrip("/"))
    filename = _safe_component(filename, f"0x{handle:08X}.bin")
    if FLAT:
        return os.path.join(OUT_DIR, filename)
    folder = posixpath.basename(posixpath.dirname(camera_path.rstrip("/")))
    camera_dir = _safe_component(camera_name, "camera")
    folder = _safe_component(folder, "unknown")
    return os.path.join(OUT_DIR, camera_dir, folder, filename)


def _unique_path(path: str) -> str:
    """Pick a non-clobbering output path."""
    if not os.path.exists(path):
        return path
    directory, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    for i in range(1, 10000):
        candidate = os.path.join(directory, f"{stem} ({i}){ext}")
        if not os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"no free filename for {path!r}")


def _set_mtime(path: str, stamp: str) -> None:
    """Set the local mtime to the camera's capture-side timestamp, if parseable."""
    if not stamp:
        return
    try:
        dt = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%S.%f")
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except (ValueError, OSError):
        pass


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
        camera_id = cli.camera_guid or cli.camera_name or ip
        camera_label = cli.camera_name or camera_id

        def process(data: bytes):
            if not data:
                return
            for rec in cli.parse_objects(cli.parse_events(data)):
                if rec.handle in done:
                    continue
                done.add(rec.handle)

                _rc, _ps, info_data = cli.get_object_info(rec.handle)
                info = cli.parse_object_info(info_data) if info_data else None
                if info is None:
                    log(
                        f"object 0x{rec.handle:08X}: no parsable 0x9170 info; "
                        "dedupe disabled for this object"
                    )
                path = rec.path or (info.filename if info else "")
                size = rec.size or (info.size if info else 0)
                if info and rec.size and info.size != rec.size:
                    log(
                        f"object 0x{rec.handle:08X}: size mismatch "
                        f"(event={rec.size}, info={info.size}); using event size"
                    )
                meta = ObjectMeta(
                    path=path,
                    size=size,
                    format=rec.format or (info.format if info else 0),
                    time1=info.time1 if info else "",
                    time2=info.time2 if info else "",
                    handle=rec.handle,
                )
                log(
                    f"object 0x{rec.handle:08X} size={size} path={path!r} "
                    f"time={meta.time1 or '?'}/{meta.time2 or '?'}"
                )
                if size <= 0:
                    log("  skipping zero-size object")
                    continue

                entry = HISTORY.lookup(camera_id, meta) if HISTORY else None
                if entry:
                    saved = entry.get("saved_as", "")
                    if REDOWNLOAD_MISSING and not os.path.exists(saved):
                        log(f"  local file missing; re-downloading ({saved})")
                    else:
                        log(f"  already downloaded; skipping ({saved})")
                        continue

                buf = cli.read_object(rec.handle, size)
                if len(buf) != size:
                    log(
                        f"  unexpected object length: got {len(buf)} of {size} bytes; "
                        "not recording in history"
                    )
                    continue
                with _output_lock:
                    fn = _unique_path(_output_path(camera_label, path, rec.handle))
                    os.makedirs(os.path.dirname(fn), exist_ok=True)
                    tmp = fn + ".part"
                    with open(tmp, "wb") as f:
                        f.write(buf)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, fn)
                _set_mtime(fn, meta.time2 or meta.time1)
                sha = hashlib.sha256(buf).hexdigest()
                head_sha = hashlib.sha256(buf[:65536]).hexdigest()
                if HISTORY:
                    HISTORY.add(camera_id, camera_label, meta, fn, sha, head_sha)
                log(
                    f"  saved {fn} ({len(buf)} bytes) "
                    f"sha256={sha} head_sha256={head_sha}"
                )

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
    global OUT_DIR, HISTORY, REDOWNLOAD_MISSING, FLAT
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface-ip", default=None)
    ap.add_argument("--http-port", type=int, default=2869)
    ap.add_argument("--out", default="downloads")
    ap.add_argument("--camera-ip", default=None, help="optional known camera IP probe")
    ap.add_argument(
        "--history",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "history.json"
        ),
        help="download history file (default: history.json next to this script)",
    )
    ap.add_argument(
        "--redownload-missing",
        action="store_true",
        help="download entries whose local file no longer exists",
    )
    ap.add_argument(
        "--flat",
        action="store_true",
        help="save as <out>/<filename> instead of <out>/<camera>/<folder>/<filename>",
    )
    args = ap.parse_args()
    OUT_DIR = args.out
    REDOWNLOAD_MISSING = args.redownload_missing
    FLAT = args.flat
    HISTORY = DownloadHistory(args.history)
    state = load_state()
    guid, pc_name = get_identity(state)
    iface_ip = args.iface_ip or guess_iface_ip()
    print(
        f"identity: guid={guid} pc_name={pc_name} iface_ip={iface_ip} "
        f"out={OUT_DIR} history={args.history}"
    )
    disc = Discovery(iface_ip, guid, pc_name, args.http_port)
    disc.camera_ip = args.camera_ip
    disc.on_camera = lambda d: on_camera(d, guid, pc_name)
    disc.run()


if __name__ == "__main__":
    main()
