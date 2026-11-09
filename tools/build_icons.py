"""Render V.A.R.A.'s icons from the SVG sources in assets/icons.

    uv run --with pillow python tools/build_icons.py

Microsoft Edge (headless) renders each SVG at 1024 px, so the output matches the design exactly; Pillow
then scales it down and packs the Windows .ico files. Re-run after changing an SVG.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ICONS = ROOT / "assets" / "icons"
WEB = ROOT / "vara" / "web"
EDGE_PATHS = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
RENDER = 1024
APP_ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
TRAY_SMALL = [16, 20, 24]  # from tray-icon-small.svg (heavier stroke)
TRAY_LARGE = [32, 40, 48, 64]  # 125%-200% display scaling, and the notification area overflow


def render(svg: Path, workdir: Path) -> Image.Image:
    edge = next((p for p in EDGE_PATHS if p.exists()), None)
    if edge is None:
        raise SystemExit("Microsoft Edge not found; it is used to render the SVGs.")
    out = workdir / (svg.stem + ".png")
    subprocess.run(
        [
            str(edge),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--default-background-color=00000000",
            f"--window-size={RENDER},{RENDER}",
            f"--user-data-dir={workdir / 'edge-profile'}",
            f"--screenshot={out}",
            svg.resolve().as_uri(),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    image = Image.open(out).convert("RGBA")
    if image.size != (RENDER, RENDER):
        raise SystemExit(f"Unexpected render size {image.size} for {svg.name}")
    return image


def scaled(image: Image.Image, size: int) -> Image.Image:
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        app = render(ICONS / "app-icon.svg", work)
        tray = render(ICONS / "tray-icon.svg", work)
        tray_small = render(ICONS / "tray-icon-small.svg", work)

    out = ICONS / "build"
    out.mkdir(exist_ok=True)
    for size in (1024, 512, 256, 192, 180, 128, 64, 48, 32):
        scaled(app, size).save(out / f"app-icon-{size}.png", optimize=True)
    for size in TRAY_SMALL:
        scaled(tray_small, size).save(out / f"tray-icon-{size}.png", optimize=True)
    for size in TRAY_LARGE:
        scaled(tray, size).save(out / f"tray-icon-{size}.png", optimize=True)

    app_frames = [scaled(app, s) for s in APP_ICO_SIZES]
    app_frames[-1].save(
        out / "app-icon.ico",
        format="ICO",
        sizes=[(s, s) for s in APP_ICO_SIZES],
        append_images=app_frames[:-1],
    )
    tray_frames = [scaled(tray_small, s) for s in TRAY_SMALL] + [
        scaled(tray, s) for s in TRAY_LARGE
    ]
    tray_frames[-1].save(
        out / "tray-icon.ico",
        format="ICO",
        sizes=[(s, s) for s in TRAY_SMALL + TRAY_LARGE],
        append_images=tray_frames[:-1],
    )

    # The chat page's browser-tab icon.
    shutil.copyfile(out / "app-icon.ico", WEB / "favicon.ico")
    shutil.copyfile(ICONS / "app-icon.svg", WEB / "favicon.svg")
    shutil.copyfile(out / "app-icon-180.png", WEB / "apple-touch-icon.png")

    for f in sorted(out.iterdir()):
        print(f"{f.relative_to(ROOT)}  {f.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
