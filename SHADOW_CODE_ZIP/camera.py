"""Lightweight Raspberry Pi camera capture for SHADOW.

Uses the Raspberry Pi camera CLI when available, so SHADOW does not need
OpenCV or another large computer-vision package just to capture one frame.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from gui import expression_management


from config import CAMERA_CAPTURE_TIMEOUT_SECONDS


def _camera_command() -> str | None:
    for name in ("rpicam-still", "libcamera-still", "fswebcam"):
        path = shutil.which(name)
        if path:
            return path
    return None


def capture_frame() -> bytes:
    """Capture one JPEG frame and return its bytes.

    Raspberry Pi OS commonly provides rpicam-still/libcamera-still. A USB
    webcam can use fswebcam if it is installed.
    """
    command = _camera_command()
    expression_management("scanning")
    if not command:
        raise RuntimeError(
            "No camera capture command found. Install/enable rpicam-still "
            "(Pi Camera) or fswebcam (USB camera)."
        )

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        output = tmp.name

    try:
        name = os.path.basename(command)
        if name in {"rpicam-still", "libcamera-still"}:
            args = [command, "-n", "-t", "500", "--width", "640", "--height", "480", "--quality", "75", "-o", output]
        else:
            args = [command, "-r", "640x480", "--no-banner", output]

        result = subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=CAMERA_CAPTURE_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Camera capture failed: {detail or result.returncode}")

        data = Path(output).read_bytes()
        if not data:
            raise RuntimeError("Camera returned an empty frame.")
        return data
    finally:
        try:
            os.unlink(output)
        except OSError:
            pass
