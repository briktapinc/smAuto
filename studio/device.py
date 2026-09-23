"""Compute device / encoder preferences. Use GPU when the machine has one."""

from __future__ import annotations

import os
from typing import Any


def cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def preferred_torch_device(*, allow_cpu_fallback: bool = True) -> str:
    """Return 'cuda' when available unless BUBBLEPOD_TTS_DEVICE / BUBBLEPOD_DEVICE forces cpu/cuda."""
    for key in ("BUBBLEPOD_TTS_DEVICE", "BUBBLEPOD_DEVICE", "LAZYKH_TTS_DEVICE"):
        raw = (os.environ.get(key) or "").strip().lower()
        if raw in ("cpu", "cuda"):
            return raw
    if cuda_available():
        return "cuda"
    return "cpu" if allow_cpu_fallback else "cuda"


def device_status() -> dict[str, Any]:
    name = ""
    ok = False
    try:
        import torch

        ok = bool(torch.cuda.is_available())
        if ok:
            try:
                name = str(torch.cuda.get_device_name(0) or "")
            except Exception:
                name = "cuda"
    except Exception:
        pass
    preferred = preferred_torch_device()
    return {
        "cuda_available": ok,
        "cuda_device_name": name,
        "preferred_device": preferred,
        "note": (
            f"Using {preferred}"
            + (f" ({name})" if ok and preferred == "cuda" else "")
            + ("" if ok else " — no NVIDIA GPU detected; CPU fallback")
        ),
    }
