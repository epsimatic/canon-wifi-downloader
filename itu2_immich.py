#!/usr/bin/env python3
"""
Minimal Immich upload client for the ITU2 emulator.

Uses only the Python standard library: HTTP is spoken through ``http.client``
and multipart bodies are assembled by hand. No Immich CLI, no Docker.

Only the endpoints needed by the emulator are implemented:

* ``GET  /api/users/me``                 - validate the API key
* ``POST /api/assets``                   - upload one asset
* ``GET  /api/albums``                   - find an album by name
* ``POST /api/albums``                   - create an album
* ``PUT  /api/albums/<id>/assets``       - add assets to an album

The API key is sent in ``x-api-key``. Immich may be mounted under a path
prefix (``https://host/immich``) and accepts both a bare URL and one ending in
``/api``; :func:`normalize_url` canonicalizes either form.
"""

from __future__ import annotations

import http.client
import json
import mimetypes
import secrets
import ssl
import time
from urllib.parse import quote, urlsplit

# Immich rejects very large bodies at the reverse proxy; this is only a guard
# against an obviously wrong argument, not an Immich limit.
_MIN_UPLOAD_TIMEOUT = 60.0
_MAX_UPLOAD_TIMEOUT = 3600.0
_UPLOAD_BYTES_PER_SECOND = 256 * 1024


class ImmichError(RuntimeError):
    """An Immich request failed.

    ``status`` is the HTTP status when the server answered. ``retryable`` marks
    transient failures (network, timeout, 5xx, 429); ``fatal`` marks credential
    problems that will not fix themselves by retrying.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        fatal: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.fatal = fatal


def normalize_url(value: str) -> str:
    """Return a canonical base URL such as ``http://host:2283``.

    Accepts ``host:port``, a trailing slash, and a trailing ``/api``.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("Immich URL is empty")
    if "://" not in value:
        value = "http://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported Immich URL scheme {parts.scheme!r}")
    if not parts.netloc:
        raise ValueError(f"Immich URL has no host: {value!r}")
    path = parts.path.rstrip("/")
    if path.endswith("/api"):
        path = path[: -len("/api")]
    # Keep a possible reverse-proxy prefix (``/immich``), drop everything else.
    return f"{parts.scheme}://{parts.netloc}{path}"


def _quote_param(value: str) -> str:
    """Escape a multipart ``Content-Disposition`` parameter value."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _describe_error(status: int, body: bytes) -> str:
    text = body.decode("utf-8", "replace").strip()
    message = ""
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        raw = parsed.get("message") or parsed.get("error")
        if isinstance(raw, list):
            message = "; ".join(str(item) for item in raw)
        elif raw:
            message = str(raw)
    if not message:
        message = text[:500] or "empty response"
    return f"HTTP {status}: {message}"


class ImmichClient:
    """Small Immich API client with upload retry and album helpers."""

    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        insecure: bool = False,
        log=print,
    ):
        self.url = normalize_url(url)
        if not (api_key or "").strip():
            raise ValueError("Immich API key is empty")
        self.api_key = api_key.strip()
        self.timeout = timeout
        self.insecure = insecure
        self.log = log
        parts = urlsplit(self.url)
        self._scheme = parts.scheme
        self._host = parts.hostname or ""
        self._port = parts.port
        self._prefix = parts.path.rstrip("/")
        self._album_ids: dict[str, str] = {}

    # -- transport ---------------------------------------------------------

    def _connect(self, timeout: float) -> http.client.HTTPConnection:
        if self._scheme == "https":
            context = (
                ssl._create_unverified_context()
                if self.insecure
                else ssl.create_default_context()
            )
            return http.client.HTTPSConnection(
                self._host, self._port, timeout=timeout, context=context
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        parts: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        """Send one request; return ``(status, body)`` for any HTTP status."""
        conn = self._connect(self.timeout if timeout is None else timeout)
        try:
            conn.putrequest(method, f"{self._prefix}/api{endpoint}")
            conn.putheader("x-api-key", self.api_key)
            conn.putheader("Accept", "application/json")
            conn.putheader("User-Agent", "itu2-emulator")
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            if parts is not None:
                conn.putheader("Content-Length", str(sum(len(part) for part in parts)))
            conn.endheaders()
            if parts:
                for part in parts:
                    conn.send(part)
            response = conn.getresponse()
            body = response.read()
            return response.status, body
        except ImmichError:
            raise
        except (OSError, http.client.HTTPException) as e:
            raise ImmichError(
                f"network error: {type(e).__name__}: {e}", retryable=True
            ) from e
        finally:
            conn.close()

    def _json(
        self,
        method: str,
        endpoint: str,
        payload: dict | list | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[int, object, bytes]:
        body: bytes | None = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = self._request(
            method,
            endpoint,
            parts=[body] if body is not None else None,
            headers=headers,
            timeout=timeout,
        )
        try:
            decoded = json.loads(raw) if raw else None
        except ValueError:
            decoded = None
        return status, decoded, raw

    def _fail(self, status: int, body: bytes, *, what: str) -> ImmichError:
        return ImmichError(
            f"{what}: {_describe_error(status, body)}",
            status=status,
            retryable=status in (408, 425, 429) or status >= 500,
            fatal=status in (401, 403),
        )

    # -- API ---------------------------------------------------------------

    def check(self) -> dict:
        """Validate the API key and return the current user."""
        status, user, raw = self._json("GET", "/users/me")
        if status != 200 or not isinstance(user, dict):
            raise self._fail(status, raw, what="auth check")
        return user

    def upload(
        self,
        data: bytes,
        filename: str,
        created_at: str,
        modified_at: str,
        *,
        device_asset_id: str = "",
        device_id: str = "itu2-emulator",
    ) -> dict:
        """Upload one file. Returns ``{"id": ..., "status": "created"|"duplicate"}``."""
        boundary = f"----itu2{secrets.token_hex(16)}"
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        fields = [
            ("filename", filename),
            ("fileCreatedAt", created_at),
            ("fileModifiedAt", modified_at),
        ]
        # Older Immich releases require these two; current ones ignore them.
        if device_asset_id:
            fields.append(("deviceAssetId", device_asset_id))
        if device_id:
            fields.append(("deviceId", device_id))

        parts: list[bytes] = []
        for name, value in fields:
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n'
                    f"\r\n"
                    f"{value}\r\n"
                ).encode("utf-8")
            )
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="assetData"; '
                f'filename="{_quote_param(filename)}"\r\n'
                f"Content-Type: {content_type}\r\n"
                f"\r\n"
            ).encode("utf-8")
        )
        parts.append(data)
        parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

        timeout = max(
            self.timeout,
            min(_MAX_UPLOAD_TIMEOUT, len(data) / _UPLOAD_BYTES_PER_SECOND),
        )
        status, body = self._request(
            "POST",
            "/assets",
            parts=parts,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            timeout=timeout,
        )
        if status not in (200, 201):
            raise self._fail(status, body, what="upload")
        try:
            result = json.loads(body)
        except ValueError as e:
            raise ImmichError(
                f"upload: invalid JSON response: {e}", status=status
            ) from e
        asset_id = result.get("id") if isinstance(result, dict) else None
        asset_status = result.get("status") if isinstance(result, dict) else None
        if not asset_id:
            raise ImmichError(f"upload: response has no asset id: {result!r}")
        return {"id": asset_id, "status": asset_status or "created"}

    def upload_with_retry(
        self,
        data: bytes,
        filename: str,
        created_at: str,
        modified_at: str,
        *,
        attempts: int = 3,
        device_asset_id: str = "",
        device_id: str = "itu2-emulator",
    ) -> dict:
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                return self.upload(
                    data,
                    filename,
                    created_at,
                    modified_at,
                    device_asset_id=device_asset_id,
                    device_id=device_id,
                )
            except ImmichError as e:
                if e.fatal or not e.retryable or attempt == attempts:
                    raise
                self.log(
                    f"immich: upload attempt {attempt}/{attempts} failed "
                    f"({e}); retrying in {delay:.0f}s"
                )
                time.sleep(delay)
                delay *= 3
        raise AssertionError("unreachable")

    def ensure_album(self, name: str) -> str:
        """Return the album id, creating the album if it does not exist."""
        name = (name or "").strip()
        if not name:
            raise ValueError("album name is empty")
        cached = self._album_ids.get(name)
        if cached:
            return cached

        status, albums, raw = self._json("GET", "/albums")
        if status != 200 or not isinstance(albums, list):
            raise self._fail(status, raw, what="list albums")
        for album in albums:
            if (
                isinstance(album, dict)
                and album.get("albumName") == name
                and album.get("id")
            ):
                self._album_ids[name] = str(album["id"])
                return self._album_ids[name]

        status, album, raw = self._json("POST", "/albums", {"albumName": name})
        if status in (200, 201) and isinstance(album, dict) and album.get("id"):
            self._album_ids[name] = str(album["id"])
            return self._album_ids[name]

        # Another client may have created it between the list and the create.
        status2, albums, _ = self._json("GET", "/albums")
        if status2 == 200 and isinstance(albums, list):
            for item in albums:
                if isinstance(item, dict) and item.get("albumName") == name:
                    self._album_ids[name] = str(item["id"])
                    return self._album_ids[name]
        raise self._fail(status, raw, what=f"create album {name!r}")

    def add_to_album(self, album_id: str, asset_ids: list[str]) -> None:
        if not asset_ids:
            return
        status, result, raw = self._json(
            "PUT",
            f"/albums/{quote(album_id, safe='')}/assets",
            {"ids": asset_ids},
        )
        if status not in (200, 201):
            raise self._fail(status, raw, what="add to album")
        if isinstance(result, list):
            for item in result:
                if not isinstance(item, dict) or item.get("success") is not False:
                    continue
                # Adding an asset that is already in the album is not an error.
                if str(item.get("error", "")).lower() == "duplicate":
                    continue
                self.log(
                    f"immich: album add failed for {item.get('id')}: "
                    f"{item.get('error') or 'unknown error'}"
                )
