#!/usr/bin/env python3
"""
AVIF conversion for downloaded camera JPEGs.

The encoder is the external ``avifenc`` tool from libavif:

    macOS          brew install libavif
    Debian/Ubuntu  apt install libavif-bin

``avifenc`` reads the JPEG from stdin, so no intermediate JPEG file is needed,
and it copies EXIF/XMP/ICC metadata from the source by default. The output is
returned as bytes and written (or uploaded) by the caller.

Runtime dependency policy: this module only uses the standard library; the
codec itself is an external binary, required only when ``--avif`` is used.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

DEFAULT_QUALITY = 55
DEFAULT_SPEED = 4
DEFAULT_TIMEOUT = 600.0


class ConversionError(RuntimeError):
    """avifenc could not encode the input."""


def find_encoder() -> str | None:
    """Return the avifenc path from ``$AVIFENC`` or ``PATH``, if usable."""
    override = os.environ.get("AVIFENC", "").strip()
    if override:
        if os.path.isfile(override) and os.access(override, os.X_OK):
            return override
        return None
    return shutil.which("avifenc")


def encoder_version(encoder: str) -> str:
    """Return a short version string such as ``1.4.2`` (empty on failure)."""
    try:
        proc = subprocess.run(
            [encoder, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (proc.stdout or proc.stderr or "").strip().splitlines()
    if not lines:
        return ""
    line = lines[0].strip()
    if line.lower().startswith("version:"):
        line = line.split(":", 1)[1].strip()
    # "1.4.2 (dav1d [dec]:1.5.4, aom [enc/dec]:3.15.0)" -> "1.4.2"
    return line.split(" (", 1)[0].strip()


def looks_like_jpeg(data: bytes) -> bool:
    """SOI marker followed by a start-of-frame marker (JPEG/JFIF/Exif)."""
    return len(data) >= 3 and data[:3] == b"\xff\xd8\xff"


def encode_avif(
    data: bytes,
    *,
    encoder: str,
    quality: int = DEFAULT_QUALITY,
    speed: int = DEFAULT_SPEED,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """Encode JPEG bytes to AVIF and return the encoded bytes.

    ``quality`` is avifenc's 0..100 color quality and ``speed`` its 0..10
    effort scale (0 is slowest). Chroma format is left at ``auto`` so the
    encoder keeps the JPEG's own subsampling instead of resampling it twice.
    """
    if not looks_like_jpeg(data):
        raise ConversionError("input is not a JPEG")

    fd, tmp = tempfile.mkstemp(prefix="itu2-avif-", suffix=".avif")
    os.close(fd)
    try:
        cmd = [
            encoder,
            "-q",
            str(quality),
            "-s",
            str(speed),
            "--stdin",
            "--input-format",
            "jpeg",
            "-o",
            tmp,
        ]
        try:
            proc = subprocess.run(
                cmd,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ConversionError(f"avifenc timed out after {timeout:g}s") from e
        except OSError as e:
            raise ConversionError(f"could not run {encoder}: {e}") from e

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()
            detail = err.splitlines()[-1] if err else f"exit code {proc.returncode}"
            raise ConversionError(detail)

        with open(tmp, "rb") as f:
            out = f.read()
        # Sanity-check the ISO BMFF header: size then 'ftyp' with an AVIF brand.
        if len(out) < 12 or out[4:8] != b"ftyp" or out[8:12] not in (b"avif", b"avis"):
            raise ConversionError("avifenc returned malformed output")
        return out
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
