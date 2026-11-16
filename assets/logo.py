# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy", "pillow"]
# ///
"""Render the havoc365 logo.

    uv run assets/logo.py

Writes assets/logo.png (static, 1024 px wide) and assets/logo.gif (animated, 440 px wide).

The mark is a gradient ribbon that runs 365 degrees, so its end overlaps its
start. Glitch slices cut through it at rest; the animation is an outage and
its recovery.
"""

from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).parent

STOPS = ["#00B4F0", "#2563EB", "#7C3AED", "#DB2777", "#FB923C"]  # start -> end of ribbon
GHOST_L, GHOST_R = "#00E5FF", "#FF2E93"  # chromatic split, left and right

R_MID, HALF_W = 0.64, 0.17  # ribbon centreline and half-width; canvas spans [-1, 1]
START, OVERLAP = 96.0, 30.0  # degrees: where the ribbon starts, how far past 360 it runs

# Glitch at rest: (top, bottom, shift) in canvas units, y pointing down.
REST = [(-0.20, -0.11, 0.07), (0.28, 0.33, -0.05)]
REST_GHOST = 0.015

TRIM = 0.88  # half-height kept on export; the ribbon reaches 0.81 and only moves sideways


def rgb(hex_):
    return np.array([int(hex_[i : i + 2], 16) for i in (1, 3, 5)]) / 255


# sRGB <-> OKLab, so the gradient stays vivid between stops.
M1 = np.array(
    [
        [0.4122214708, 0.5363325363, 0.0514459929],
        [0.2119034982, 0.6806995451, 0.1073969566],
        [0.0883024619, 0.2817188376, 0.6299787005],
    ]
)
M2 = np.array(
    [
        [0.2104542553, 0.7936177850, -0.0040720468],
        [1.9779984951, -2.4285922050, 0.4505937099],
        [0.0259040371, 0.7827717662, -0.8086757660],
    ]
)


def to_oklab(c):
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return np.cbrt(lin @ M1.T) @ M2.T


def from_oklab(lab):
    lin = ((lab @ np.linalg.inv(M2).T) ** 3) @ np.linalg.inv(M1).T
    lin = np.clip(lin, 0, 1)
    return np.where(lin <= 0.0031308, 12.92 * lin, 1.055 * lin ** (1 / 2.4) - 0.055)


def gradient(n=1024):
    stops = to_oklab(np.array([rgb(h) for h in STOPS]))
    x = np.linspace(0, 1, n) * (len(STOPS) - 1)
    i = np.minimum(x.astype(int), len(STOPS) - 2)
    f = (x - i)[:, None]
    return from_oklab(stops[i] * (1 - f) + stops[i + 1] * f)


def render(size):
    """The clean ribbon as float RGBA, with analytic anti-aliasing."""
    px = 2 / size
    c = (np.arange(size) + 0.5) * px - 1
    x, y = np.meshgrid(c, -c)  # y up
    r = np.hypot(x, y)
    travelled = (START - np.degrees(np.arctan2(y, x))) % 360  # clockwise from start
    total = 360 + OVERLAP

    # The head runs over the first OVERLAP degrees and ends in a round cap.
    end = np.radians(START - OVERLAP)
    d_cap = np.hypot(x - R_MID * np.cos(end), y - R_MID * np.sin(end)) - HALF_W
    head = np.maximum(travelled <= OVERLAP, np.clip(0.5 - d_cap / px, 0, 1))[..., None]

    lut = gradient()
    pick = lambda deg: lut[(np.clip(deg / total, 0, 1) * (len(lut) - 1)).astype(int)]
    tail = pick(travelled)
    tail *= 1 - (0.38 * np.clip(1 - d_cap / 0.11, 0, 1) ** 2 * (travelled > OVERLAP))[..., None]
    colour = tail * (1 - head) + pick(360 + travelled) * head

    u = np.clip((r - R_MID + HALF_W) / (2 * HALF_W), 0, 1)  # 0 inner edge, 1 outer edge
    colour *= (0.93 + 0.11 * np.sin(np.pi * u) + 0.05 * (y - x) / np.sqrt(2))[..., None]

    alpha = np.clip(0.5 - (np.abs(r - R_MID) - HALF_W) / px, 0, 1)
    return np.dstack([np.clip(colour, 0, 1), alpha])


def over(top, bottom):
    a, b = top[..., 3:], bottom[..., 3:]
    out = a + b * (1 - a)
    mixed = (top[..., :3] * a + bottom[..., :3] * b * (1 - a)) / np.maximum(out, 1e-6)
    return np.dstack([mixed, out])


def shift_x(img, k):
    out = np.zeros_like(img)
    if k > 0:
        out[:, k:] = img[:, :-k]
    elif k < 0:
        out[:, :k] = img[:, -k:]
    else:
        out[:] = img
    return out


def tint(img, hex_, strength=0.9):
    return np.dstack([np.broadcast_to(rgb(hex_), img.shape[:2] + (3,)), img[..., 3] * strength])


def glitch(img, bands, ghost=0.0, jitter=0.0, drops=(), desat=0.0):
    """Shift horizontal bands, split their colour, drop slices, drain colour."""
    n = img.shape[0]
    unit = lambda v: round(v * n / 2)
    row = lambda v: int(np.clip(round((v + 1) * n / 2), 0, n))
    out = img.copy()
    for top, bottom, shift in bands:
        a, b = row(top), row(bottom)
        band = shift_x(img[a:b], unit(shift))
        if ghost:
            g = unit(ghost)
            split = over(tint(shift_x(band, -g), GHOST_L), tint(shift_x(band, g), GHOST_R))
            band = over(band, split)
        out[a:b] = band
    for top, bottom in drops:
        out[row(top) : row(bottom), :, 3] = 0
    if desat:
        grey = (out[..., :3] @ np.array([0.2126, 0.7152, 0.0722]))[..., None]
        out[..., :3] = out[..., :3] * (1 - desat) + (0.35 + 0.4 * grey) * desat
    return shift_x(out, unit(jitter))


def burst(base, rng, intensity):
    bands = [(t, b, s * (1 + 2 * intensity)) for t, b, s in REST]
    for _ in range(2 + int(5 * intensity)):
        top = rng.uniform(-0.85, 0.80)
        height = rng.uniform(0.02, 0.10)
        shift = rng.choice([-1, 1]) * rng.uniform(0.02, 0.04 + 0.11 * intensity)
        bands.append((top, top + height, shift))
    drops = []
    if intensity > 0.6:
        for _ in range(rng.integers(1, 3)):
            top = rng.uniform(-0.8, 0.75)
            drops.append((top, top + rng.uniform(0.02, 0.06)))
    return glitch(
        base,
        bands,
        ghost=REST_GHOST + 0.035 * intensity,
        jitter=rng.uniform(-0.02, 0.02) * intensity,
        drops=drops,
        desat=0.8 * intensity,
    )


def animation(size):
    base = render(size)
    rest = glitch(base, REST, ghost=REST_GHOST)
    rng = np.random.default_rng(365)
    frames = [(rest, 2000)]
    for t in (np.arange(16) + 0.5) / 16:  # outage
        frames.append((burst(base, rng, np.sin(np.pi * t) ** 0.7), 40))
    frames.append((rest, 900))
    for t in (np.arange(3) + 0.5) / 3:  # aftershock
        frames.append((burst(base, rng, 0.25 * np.sin(np.pi * t)), 40))
    return frames


def save_png(img, path):
    Image.fromarray(np.round(img * 255).astype(np.uint8), "RGBA").save(path, optimize=True)


def save_gif(frames, path):
    """Each frame gets its own 255-colour palette; index 255 is transparent."""
    images = []
    for f, _ in frames:
        f = np.round(f * 255).astype(np.uint8)
        solid = f[..., 3] >= 128
        sample = Image.fromarray(f[..., :3][solid][None])
        colours = sample.quantize(colors=255, method=Image.Quantize.MEDIANCUT).getpalette()[: 255 * 3]
        colours += colours[:3] * (256 - len(colours) // 3)  # pad by repeating slot 0
        palette = Image.new("P", (1, 1))
        palette.putpalette(colours)
        idx = np.array(Image.fromarray(f[..., :3]).quantize(palette=palette, dither=Image.Dither.NONE))
        idx[idx == 255] = 0
        idx[~solid] = 255
        im = Image.fromarray(idx, "P")
        im.putpalette(colours[: 255 * 3] + [0, 0, 0])
        images.append(im)
    images[0].save(
        path,
        save_all=True,
        append_images=images[1:],
        duration=[d for _, d in frames],
        loop=0,
        transparency=255,
        disposal=2,
        optimize=False,
    )


def trim(img):
    m = round((1 - TRIM) * img.shape[0] / 2)
    return img[m:-m]


if __name__ == "__main__":
    save_png(trim(glitch(render(1024), REST, ghost=REST_GHOST)), HERE / "logo.png")
    save_gif([(trim(f), d) for f, d in animation(440)], HERE / "logo.gif")
