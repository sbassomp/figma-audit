"""Color conversion and comparison utilities."""

from __future__ import annotations

import math


def rgba_to_hex(r: float, g: float, b: float, a: float = 1.0) -> str:
    """Convert Figma RGBA (0-1 floats) to hex string."""
    ri = min(255, max(0, round(r * 255)))
    gi = min(255, max(0, round(g * 255)))
    bi = min(255, max(0, round(b * 255)))
    if a < 1.0:
        ai = min(255, max(0, round(a * 255)))
        return f"#{ri:02X}{gi:02X}{bi:02X}{ai:02X}"
    return f"#{ri:02X}{gi:02X}{bi:02X}"


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color to RGB tuple (0-255)."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _srgb_to_linear(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_xyz(r: float, g: float, b: float) -> tuple[float, float, float]:
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    return x, y, z


def _xyz_to_lab(x: float, y: float, z: float) -> tuple[float, float, float]:
    # D65 illuminant
    xn, yn, zn = 0.95047, 1.0, 1.08883
    x, y, z = x / xn, y / yn, z / zn

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    lab_l = 116 * f(y) - 16
    a = 500 * (f(x) - f(y))
    b = 200 * (f(y) - f(z))
    return lab_l, a, b


def rgb_to_lab(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert sRGB (0-255) to CIELAB."""
    rl = _srgb_to_linear(float(r))
    gl = _srgb_to_linear(float(g))
    bl = _srgb_to_linear(float(b))
    x, y, z = _linear_to_xyz(rl, gl, bl)
    return _xyz_to_lab(x, y, z)


def delta_e_2000(lab1: tuple[float, float, float], lab2: tuple[float, float, float]) -> float:
    """CIE2000 color difference (deltaE00)."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2

    c1 = math.sqrt(a1**2 + b1**2)
    c2 = math.sqrt(a2**2 + b2**2)
    avg_c = (c1 + c2) / 2

    g = 0.5 * (1 - math.sqrt(avg_c**7 / (avg_c**7 + 25**7)))
    a1p = a1 * (1 + g)
    a2p = a2 * (1 + g)
    c1p = math.sqrt(a1p**2 + b1**2)
    c2p = math.sqrt(a2p**2 + b2**2)

    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360

    dl = l2 - l1
    dc = c2p - c1p

    if c1p * c2p == 0:
        dh = 0
    elif abs(h2p - h1p) <= 180:
        dh = h2p - h1p
    elif h2p - h1p > 180:
        dh = h2p - h1p - 360
    else:
        dh = h2p - h1p + 360

    dh_val = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dh / 2))

    avg_lp = (l1 + l2) / 2
    avg_cp = (c1p + c2p) / 2

    if c1p * c2p == 0:
        avg_hp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        avg_hp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        avg_hp = (h1p + h2p + 360) / 2
    else:
        avg_hp = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(avg_hp - 30))
        + 0.24 * math.cos(math.radians(2 * avg_hp))
        + 0.32 * math.cos(math.radians(3 * avg_hp + 6))
        - 0.20 * math.cos(math.radians(4 * avg_hp - 63))
    )

    sl = 1 + 0.015 * (avg_lp - 50) ** 2 / math.sqrt(20 + (avg_lp - 50) ** 2)
    sc = 1 + 0.045 * avg_cp
    sh = 1 + 0.015 * avg_cp * t

    rt = (
        -2
        * math.sqrt(avg_cp**7 / (avg_cp**7 + 25**7))
        * math.sin(math.radians(60 * math.exp(-(((avg_hp - 275) / 25) ** 2))))
    )

    de = math.sqrt(
        (dl / sl) ** 2 + (dc / sc) ** 2 + (dh_val / sh) ** 2 + rt * (dc / sc) * (dh_val / sh)
    )
    return de


def color_distance(hex1: str, hex2: str) -> float:
    """Delta E (CIE2000) between two hex colors."""
    r1, g1, b1 = hex_to_rgb(hex1)
    r2, g2, b2 = hex_to_rgb(hex2)
    lab1 = rgb_to_lab(r1, g1, b1)
    lab2 = rgb_to_lab(r2, g2, b2)
    return delta_e_2000(lab1, lab2)
