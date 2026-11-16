# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow"]
# ///
"""Record the map demo and give each evidence screen time to be read."""

import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageSequence, ImageStat

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    subprocess.run(["vhs", "assets/map.tape"], cwd=ROOT, check=True)
    target = ROOT / "assets" / "map.gif"
    with Image.open(target) as animation:
        frames = [frame.convert("RGB") for frame in ImageSequence.Iterator(animation)]
    durations = [80] * len(frames)
    durations[-1] = 1000
    # Headless screen capture may drop unchanged frames, including VHS's Sleep holds.
    for name in ("map.png", "map-detail.png", "map-limits.png"):
        with Image.open(ROOT / "assets" / name) as screenshot:
            expected = screenshot.convert("RGB").resize((110, 105))
        closest = min(
            range(len(frames)),
            key=lambda index: sum(
                ImageStat.Stat(
                    ImageChops.difference(frames[index].resize((110, 105)), expected)
                ).mean
            ),
        )
        durations[closest] = 3000
    frames[0].save(
        target, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True
    )
    (ROOT / "assets" / "map-detail.png").unlink()
    (ROOT / "assets" / "map-limits.png").unlink()


if __name__ == "__main__":
    main()
