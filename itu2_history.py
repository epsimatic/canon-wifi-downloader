#!/usr/bin/env python3
"""
Persistent download history for the ITU2 emulator.

The camera does not expose a content checksum. What it does expose before a
download (via the 0xC1AB event and the 0x9170 info record) is enough to identify
an object across sessions: full camera path, size, format, and two camera-side
timestamps with one-second resolution. A new photo that reuses an old filename
will normally differ in at least one of those fields.

Entries are additionally keyed by the camera GUID so that two cameras with the
same folder/filename scheme do not share history.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass

HISTORY_VERSION = 1


@dataclass(frozen=True)
class ObjectMeta:
    """Metadata available before downloading an object."""

    path: str
    size: int
    format: int = 0
    time1: str = ""
    time2: str = ""
    handle: int = 0


def object_key(meta: ObjectMeta) -> str:
    """Stable key for one camera-side object."""
    raw = "\x00".join(
        (meta.path, str(meta.size), str(meta.format), meta.time1, meta.time2)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DownloadHistory:
    def __init__(self, path: str, log=print):
        self.path = path
        self.log = log
        self._lock = threading.Lock()
        self._data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("history root is not an object")
            if data.get("version") != HISTORY_VERSION:
                raise ValueError(f"unsupported history version {data.get('version')!r}")
            if not isinstance(data.get("cameras"), dict):
                raise ValueError("history has no cameras map")
            return data
        except FileNotFoundError:
            return {"version": HISTORY_VERSION, "cameras": {}}
        except Exception as e:
            backup = f"{self.path}.corrupt-{int(time.time())}"
            try:
                os.replace(self.path, backup)
                self.log(f"history: {e}; moved aside to {backup}")
            except Exception:
                self.log(f"history: {e}; starting with empty history")
            return {"version": HISTORY_VERSION, "cameras": {}}

    def _camera(self, camera_id: str, camera_name: str = "") -> dict:
        cameras = self._data["cameras"]
        camera = cameras.setdefault(camera_id, {"name": camera_name, "objects": {}})
        if camera_name and not camera.get("name"):
            camera["name"] = camera_name
        return camera

    def lookup(self, camera_id: str, meta: ObjectMeta) -> dict | None:
        """Return the stored entry, or None if this object is new."""
        # Without camera timestamps the key is too weak: a new photo that reuses
        # a name and size could be skipped. In that case, treat it as new.
        if not meta.time1 and not meta.time2:
            return None
        with self._lock:
            camera = self._data["cameras"].get(camera_id)
            if not camera:
                return None
            return camera.get("objects", {}).get(object_key(meta))

    def add(
        self,
        camera_id: str,
        camera_name: str,
        meta: ObjectMeta,
        saved_as: str,
        sha256: str,
        head_sha256: str = "",
    ) -> None:
        if not meta.time1 and not meta.time2:
            self.log(f"history: not recording {meta.path!r} (no camera timestamps)")
            return
        with self._lock:
            camera = self._camera(camera_id, camera_name)
            camera["objects"][object_key(meta)] = {
                "path": meta.path,
                "size": meta.size,
                "format": meta.format,
                "handle": f"0x{meta.handle:08X}",
                "time1": meta.time1,
                "time2": meta.time2,
                "sha256": sha256,
                "head_sha256": head_sha256,
                "saved_as": os.path.abspath(saved_as),
                "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            try:
                self._save()
            except OSError as e:
                self.log(f"history: failed to save {self.path}: {e}")

    def _save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
