#!/usr/bin/env python3
"""
ITU2 emulator — discovery + pairing + auto-send transfer.

Advertises the Computer device, and when the camera appears:
  * pairing mode  -> completes the pairing handshake
  * transfer mode -> downloads every object the camera announces

Usage:
    python3 itu2.py [--iface-ip 192.0.2.10] [--http-port 2869] [--debug]
                    [-d downloads | -i http://immich.local:2283]
                    [--immich-key KEY | IMMICH_API_KEY=KEY] [--immich-album NAME]
                    [--flat] [--avif [--avif-quality Q] [--avif-speed S]]
                    [--keep-original]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import posixpath
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from itu2_convert import (
    DEFAULT_QUALITY as AVIF_DEFAULT_QUALITY,
    DEFAULT_SPEED as AVIF_DEFAULT_SPEED,
    ConversionError,
    encode_avif,
    encoder_version,
    find_encoder,
    looks_like_jpeg,
)  # noqa: E402
from itu2_discovery import (
    STATE_PATH,
    Discovery,
    get_identity,
    guess_iface_ip,
    load_state,
    save_state,
)  # noqa: E402
from itu2_history import DownloadHistory, ObjectMeta  # noqa: E402
from itu2_immich import ImmichClient, ImmichError  # noqa: E402
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
CAPTURE_DIR: str | None = None
CHUNK_SIZE = 0x200000
RCVBUF = 0
MODE = "dir"  # "dir" or "immich"
IMMICH: ImmichClient | None = None
IMMICH_ALBUM: str | None = None
DEBUG = False
AVIF = False
AVIF_QUALITY = AVIF_DEFAULT_QUALITY
AVIF_SPEED = AVIF_DEFAULT_SPEED
AVIF_KEEP_ORIGINAL = False
AVIFENC: str | None = None
AVIFENC_VERSION = ""


_UNSAFE_CHARS = set("/\\:*?\"<>|'")
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _safe_component(name: str, fallback: str) -> str:
    """Make one path component safe on Linux/macOS and Windows.

    Separators, quotes, colon and other Windows-reserved characters are
    replaced with ``_``. Every non-printable code point is also replaced with
    ``_``. Names that reduce to empty, ``.`` or ``..`` use the fallback, and
    Windows device names (CON, NUL, COM1, ...) are prefixed with ``_``.
    """
    cleaned = []
    for ch in name or "":
        if ch in _UNSAFE_CHARS or not ch.isprintable():
            cleaned.append("_")
        else:
            cleaned.append(ch)
    name = "".join(cleaned).strip(" .")
    if name in ("", ".", ".."):
        return fallback
    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED_NAMES:
        name = f"_{name}"
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


def _write_atomic(path: str, data: bytes) -> None:
    """Write ``data`` to ``path`` via a ``.part`` file and an atomic rename."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _with_avif_ext(camera_path: str, handle: int) -> str:
    """Camera path with its extension replaced by ``.avif``."""
    if not (camera_path or "").strip("/"):
        return f"0x{handle:08X}.avif"
    stem, _ext = posixpath.splitext(camera_path)
    return stem + ".avif"


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


def _upload_filename(camera_path: str, handle: int) -> str:
    """Original camera filename, safe as a multipart parameter value."""
    name = posixpath.basename((camera_path or "").rstrip("/"))
    name = "".join("_" if not ch.isprintable() else ch for ch in name)
    if name in ("", ".", ".."):
        return f"0x{handle:08X}.bin"
    return name


def _iso_camera_time(stamp: str) -> str | None:
    """Convert ``YYYYMMDDTHHMMSS.fff`` camera wall time to local ISO 8601."""
    if not stamp:
        return None
    for fmt in ("%Y%m%dT%H%M%S.%f", "%Y%m%dT%H%M%S"):
        try:
            dt = datetime.datetime.strptime(stamp, fmt)
        except ValueError:
            continue
        # The camera clock is local wall time; attach this computer's offset.
        return dt.astimezone().isoformat(timespec="milliseconds")
    return None


def _upload_timestamps(meta: ObjectMeta) -> tuple[str, str]:
    iso = _iso_camera_time(meta.time2 or meta.time1)
    if iso is None:
        iso = datetime.datetime.now().astimezone().isoformat(timespec="milliseconds")
    return iso, iso


def _saved_immich_key(state: dict) -> str:
    """Return the Immich API key stored in state.json, if any."""
    immich = state.get("immich")
    if isinstance(immich, dict):
        key = immich.get("api_key")
        if isinstance(key, str):
            return key.strip()
    return ""


def _store_immich_key(state: dict, key: str) -> None:
    """Persist an Immich API key in state.json (written with mode 0600)."""
    immich = state.get("immich")
    if not isinstance(immich, dict):
        immich = {}
        state["immich"] = immich
    immich["api_key"] = key
    save_state(state)


def _forget_immich_key(state: dict) -> bool:
    """Remove the saved Immich API key; return whether one was present."""
    immich = state.get("immich")
    if not isinstance(immich, dict) or "api_key" not in immich:
        return False
    del immich["api_key"]
    if not immich:
        state.pop("immich", None)
    save_state(state)
    return True


def _line(ip: str, *a):
    print(f"[{time.strftime('%H:%M:%S')}][{ip}]", *a, flush=True)


def _debug_line(ip: str, *a):
    if DEBUG:
        print(f"[{time.strftime('%H:%M:%S')}][{ip}]", *a, flush=True)


def session(ip: str, guid: str, pc_name: str, mode: str):
    def log(*a):
        _line(ip, *a)

    def dbg(*a):
        _debug_line(ip, *a)

    cli = None
    try:
        cli = PTPIPClient(
            ip,
            guid,
            pc_name,
            log=log,
            debug_log=dbg,
            save_dir=CAPTURE_DIR,
            rcvbuf=RCVBUF,
        )
        cli.connect()
        setup = cli.start_session()
        if mode == "Pairing":
            log("completing pairing…")
            cli.finish_pairing()
            log("pairing done")
            return
        # Transfer
        if MODE == "immich" and IMMICH is None:
            raise RuntimeError(
                "Immich destination selected but no client is configured"
            )
        if MODE == "dir":
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
                    dbg(
                        f"object 0x{rec.handle:08X}: no parsable 0x9170 info; "
                        "dedupe disabled for this object"
                    )
                path = rec.path or (info.filename if info else "")
                size = rec.size or (info.size if info else 0)
                if info and rec.size and info.size != rec.size:
                    dbg(
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
                dbg(
                    f"object 0x{rec.handle:08X} size={size} path={path!r} "
                    f"time={meta.time1 or '?'}/{meta.time2 or '?'}"
                )
                if size <= 0:
                    log("  skipping zero-size object")
                    continue

                entry = HISTORY.lookup(camera_id, meta) if HISTORY else None
                if MODE == "dir":
                    saved = (entry or {}).get("saved_as", "")
                    if saved:
                        if REDOWNLOAD_MISSING and not os.path.exists(saved):
                            log(f"  local file missing; re-downloading ({saved})")
                        else:
                            dbg(f"  already downloaded; skipping ({saved})")
                            continue
                else:
                    remote = ((entry or {}).get("immich") or {}).get(IMMICH.url)
                    if remote:
                        dbg(
                            "  already uploaded; skipping "
                            f"(asset {remote.get('asset_id', '?')})"
                        )
                        continue

                buf = cli.read_object(
                    rec.handle,
                    size,
                    chunk=CHUNK_SIZE,
                    storage_id=info.storage_id if info else 0x00020001,
                )
                if len(buf) != size:
                    log(
                        f"  unexpected object length: got {len(buf)} of {size} bytes; "
                        "not recording in history"
                    )
                    continue
                sha = hashlib.sha256(buf).hexdigest()
                head_sha = hashlib.sha256(buf[:65536]).hexdigest()

                payload = buf
                out_path = path
                converted: dict | None = None
                if AVIF and looks_like_jpeg(buf):
                    try:
                        avif = encode_avif(
                            buf,
                            encoder=AVIFENC or "avifenc",
                            quality=AVIF_QUALITY,
                            speed=AVIF_SPEED,
                        )
                    except ConversionError as e:
                        log(f"  avif conversion failed; keeping JPEG: {e}")
                    else:
                        payload = avif
                        out_path = _with_avif_ext(path, rec.handle)
                        converted = {
                            "format": "avif",
                            "encoder": "avifenc",
                            "version": AVIFENC_VERSION,
                            "quality": AVIF_QUALITY,
                            "speed": AVIF_SPEED,
                            "sha256": hashlib.sha256(avif).hexdigest(),
                            "size": len(avif),
                        }
                        log(
                            f"  avif: {len(buf)} -> {len(avif)} bytes "
                            f"({len(avif) * 100.0 / len(buf):.1f}% of JPEG)"
                        )
                elif AVIF:
                    dbg(f"  not a JPEG ({path!r}); keeping the original object")

                if MODE == "dir":
                    with _output_lock:
                        if converted is not None and AVIF_KEEP_ORIGINAL:
                            fn = _unique_path(
                                _output_path(camera_label, path, rec.handle)
                            )
                            avif_fn = os.path.splitext(fn)[0] + ".avif"
                            if os.path.exists(avif_fn):
                                avif_fn = _unique_path(avif_fn)
                            _write_atomic(fn, buf)
                            _write_atomic(avif_fn, payload)
                            converted["original_path"] = fn
                            fn = avif_fn
                        else:
                            fn = _unique_path(
                                _output_path(camera_label, out_path, rec.handle)
                            )
                            _write_atomic(fn, payload)
                    _set_mtime(fn, meta.time2 or meta.time1)
                    if converted is not None:
                        converted["path"] = fn
                        if AVIF_KEEP_ORIGINAL:
                            _set_mtime(
                                converted["original_path"], meta.time2 or meta.time1
                            )
                    if HISTORY:
                        HISTORY.add(
                            camera_id,
                            camera_label,
                            meta,
                            fn,
                            sha,
                            head_sha,
                            converted=converted,
                        )
                    log(f"  saved {fn} ({len(payload)} bytes)")
                    dbg(f"  sha256={sha} head_sha256={head_sha}")
                    continue

                filename = _upload_filename(out_path, rec.handle)
                created, modified = _upload_timestamps(meta)
                log(f"  uploading {filename} ({len(payload)} bytes)…")
                try:
                    result = IMMICH.upload_with_retry(
                        payload,
                        filename,
                        created,
                        modified,
                        device_asset_id=(
                            f"{camera_label}/{path}".strip("/") or f"0x{rec.handle:08X}"
                        ),
                    )
                except ImmichError as e:
                    log(f"  immich upload failed; not recorded: {e}")
                    if e.fatal:
                        log("  Immich rejected the credentials; stopping this session")
                        return
                    continue
                asset_id = result["id"]
                if converted is not None:
                    converted["filename"] = filename
                if HISTORY:
                    HISTORY.add_immich(
                        camera_id,
                        camera_label,
                        meta,
                        IMMICH.url,
                        asset_id,
                        result["status"],
                        sha,
                        head_sha,
                        converted=converted,
                    )
                log(
                    f"  uploaded {filename} ({len(payload)} bytes) -> asset {asset_id} "
                    f"({result['status']})"
                )
                dbg(f"  sha256={sha}")
                if IMMICH_ALBUM:
                    try:
                        album_id = IMMICH.ensure_album(IMMICH_ALBUM)
                        IMMICH.add_to_album(album_id, [asset_id])
                    except ImmichError as e:
                        log(f"  immich album error: {e}")

        for _name, _rc, _ps, data in setup:  # includes the initial 0x9116 object table
            process(data)
        for _ in range(60):
            process(cli.get_event())
            time.sleep(2)
        dbg("transfer tail")
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
    global OUT_DIR, HISTORY, REDOWNLOAD_MISSING, FLAT, CAPTURE_DIR, CHUNK_SIZE, RCVBUF
    global MODE, IMMICH, IMMICH_ALBUM, DEBUG
    global AVIF, AVIF_QUALITY, AVIF_SPEED, AVIF_KEEP_ORIGINAL, AVIFENC, AVIFENC_VERSION
    ap = argparse.ArgumentParser(
        epilog=(
            "Immich API key permissions: asset.upload (uploads); album.read, "
            "album.create, albumAsset.create, asset.share (with --immich-album); "
            "user.read optional (shows the account name)."
        )
    )
    ap.add_argument("--iface-ip", default=None)
    ap.add_argument("--http-port", type=int, default=2869)
    ap.add_argument(
        "--debug",
        action="store_true",
        help="show verbose protocol tracing (SSDP, PTP/IP packets, chunk reads)",
    )
    destination = ap.add_mutually_exclusive_group()
    destination.add_argument(
        "-d",
        "--dir",
        "--out",
        dest="dir",
        default=None,
        metavar="DIR",
        help="write files under DIR (default: downloads)",
    )
    destination.add_argument(
        "-i",
        "--immich",
        metavar="URL",
        default=None,
        help="upload files to an Immich instance instead of writing them to disk",
    )
    ap.add_argument(
        "--immich-key",
        default=None,
        metavar="KEY",
        help="Immich API key (default: IMMICH_API_KEY, then a key saved in state.json)",
    )
    ap.add_argument(
        "--immich-album",
        default=None,
        metavar="NAME",
        help="add every uploaded asset to this album, creating it if needed",
    )
    ap.add_argument(
        "--immich-insecure",
        action="store_true",
        help="skip TLS certificate verification for Immich (self-signed HTTPS)",
    )
    ap.add_argument(
        "--immich-save-key",
        action="store_true",
        help="store the Immich API key in state.json for future runs (mode 0600)",
    )
    ap.add_argument(
        "--immich-forget-key",
        action="store_true",
        help="remove the saved Immich API key from state.json",
    )
    ap.add_argument("--camera-ip", default=None, help="optional known camera IP probe")
    ap.add_argument(
        "--history",
        default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "history.json"
        ),
        help="download/upload history file (default: history.json next to this script)",
    )
    ap.add_argument(
        "--redownload-missing",
        action="store_true",
        help="download entries whose local file no longer exists",
    )
    ap.add_argument(
        "--flat",
        action="store_true",
        help="save as <dir>/<filename> instead of <dir>/<camera>/<folder>/<filename>",
    )
    ap.add_argument(
        "--avif",
        action="store_true",
        help=(
            "convert JPEG objects to AVIF (q55, speed 4, source chroma) and use "
            ".avif as the stored/uploaded file; requires avifenc from libavif"
        ),
    )
    ap.add_argument(
        "--avif-quality",
        type=int,
        default=None,
        metavar="Q",
        help="avifenc color quality 0..100 (default: 55; requires --avif)",
    )
    ap.add_argument(
        "--avif-speed",
        type=int,
        default=None,
        metavar="S",
        help="avifenc speed 0..10, 0 slowest (default: 4; requires --avif)",
    )
    ap.add_argument(
        "--keep-original",
        action="store_true",
        help="with --avif and --dir, keep the JPEG next to the AVIF",
    )
    ap.add_argument(
        "--chunk-size",
        type=int,
        default=0x200000,
        help="object read chunk size in bytes (default: 2097152)",
    )
    ap.add_argument(
        "--capture",
        default=None,
        help="save raw operation payloads to this directory (debug, slow)",
    )
    ap.add_argument(
        "--rcvbuf",
        type=int,
        default=0,
        help="socket receive buffer in bytes; 0 uses the OS default",
    )
    args = ap.parse_args()
    DEBUG = args.debug
    if args.chunk_size <= 0:
        ap.error("--chunk-size must be positive")
    if args.rcvbuf < 0:
        ap.error("--rcvbuf must be zero or positive")

    quality = AVIF_DEFAULT_QUALITY if args.avif_quality is None else args.avif_quality
    speed = AVIF_DEFAULT_SPEED if args.avif_speed is None else args.avif_speed
    if args.avif_quality is not None and not args.avif:
        ap.error("--avif-quality requires --avif")
    if args.avif_speed is not None and not args.avif:
        ap.error("--avif-speed requires --avif")
    if args.keep_original and not args.avif:
        ap.error("--keep-original requires --avif")
    if args.keep_original and args.immich:
        ap.error("--keep-original is only valid with --dir")
    if not 0 <= quality <= 100:
        ap.error("--avif-quality must be between 0 and 100")
    if not 0 <= speed <= 10:
        ap.error("--avif-speed must be between 0 and 10")
    if args.avif:
        AVIFENC = find_encoder()
        if not AVIFENC:
            ap.error(
                "--avif requires avifenc from libavif. Install it with "
                "'brew install libavif' (macOS) or 'apt install libavif-bin' "
                "(Debian/Ubuntu), set AVIFENC to its path, or run without --avif."
            )
        AVIFENC_VERSION = encoder_version(AVIFENC)

    state = load_state()

    if args.immich_save_key and not args.immich:
        ap.error("--immich-save-key requires --immich")

    if args.immich_forget_key:
        try:
            removed = _forget_immich_key(state)
        except OSError as e:
            ap.error(f"could not update {STATE_PATH}: {e}")
        if removed:
            print(f"immich: removed saved API key from {STATE_PATH}")
        else:
            print(f"immich: no saved API key in {STATE_PATH}")
        if not args.immich:
            return

    if args.immich:
        if args.redownload_missing:
            ap.error("--redownload-missing is only valid with --dir")
        if args.flat:
            ap.error("--flat is only valid with --dir")
        key = (args.immich_key or os.environ.get("IMMICH_API_KEY", "")).strip()
        if not key:
            key = _saved_immich_key(state)
            if key:
                print(f"immich: using saved API key from {STATE_PATH}")
        if not key:
            ap.error(
                "--immich requires --immich-key, IMMICH_API_KEY, or a saved "
                "key (run once with --immich-save-key)"
            )
        try:
            client = ImmichClient(args.immich, key, insecure=args.immich_insecure)
        except ValueError as e:
            ap.error(str(e))
        try:
            user = client.check()
        except ImmichError as e:
            # 403 means the key is valid but lacks ``user.read``. Uploading can
            # still work, so only reject bad credentials (401) and real errors.
            if e.status == 403:
                user = {}
                print(f"immich: API key accepted; account details unavailable ({e})")
            else:
                ap.error(f"Immich check failed for {client.url}: {e}")
        if args.immich_save_key:
            try:
                _store_immich_key(state, key)
            except OSError as e:
                ap.error(f"could not save Immich API key to {STATE_PATH}: {e}")
            print(f"immich: API key saved to {STATE_PATH} (mode 0600)")
        MODE = "immich"
        IMMICH = client
        IMMICH_ALBUM = args.immich_album
        print(
            f"immich: url={client.url} "
            f"user={user.get('email') or user.get('name') or '?'} "
            f"album={IMMICH_ALBUM or '-'}"
        )
        if IMMICH_ALBUM:
            try:
                album_id = client.ensure_album(IMMICH_ALBUM)
            except ImmichError as e:
                ap.error(f"Immich album {IMMICH_ALBUM!r} is not usable: {e}")
            print(f"immich: album {IMMICH_ALBUM!r} id={album_id}")
    else:
        if args.immich_key:
            ap.error("--immich-key requires --immich")
        if args.immich_album:
            ap.error("--immich-album requires --immich")
        if args.immich_insecure:
            ap.error("--immich-insecure requires --immich")
        MODE = "dir"
        OUT_DIR = args.dir or "downloads"

    REDOWNLOAD_MISSING = args.redownload_missing
    FLAT = args.flat
    CAPTURE_DIR = args.capture
    CHUNK_SIZE = args.chunk_size
    RCVBUF = args.rcvbuf
    AVIF = args.avif
    AVIF_QUALITY = quality
    AVIF_SPEED = speed
    AVIF_KEEP_ORIGINAL = args.keep_original
    HISTORY = DownloadHistory(args.history)
    guid, pc_name = get_identity(state)
    iface_ip = args.iface_ip or guess_iface_ip()
    dest = f"immich={IMMICH.url}" if MODE == "immich" else f"out={OUT_DIR}"
    avif_info = ""
    if AVIF:
        avif_info = (
            f" avif=q{AVIF_QUALITY}/s{AVIF_SPEED} encoder={AVIFENC_VERSION or AVIFENC}"
        )
        if AVIF_KEEP_ORIGINAL:
            avif_info += " keep-original"
    print(
        f"identity: guid={guid} pc_name={pc_name} iface_ip={iface_ip} "
        f"{dest} history={args.history}{avif_info}"
    )
    disc = Discovery(iface_ip, guid, pc_name, args.http_port, debug=args.debug)
    disc.camera_ip = args.camera_ip
    disc.on_camera = lambda d: on_camera(d, guid, pc_name)
    disc.run()


if __name__ == "__main__":
    main()
