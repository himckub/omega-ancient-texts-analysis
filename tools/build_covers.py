#!/usr/bin/env python3
"""Generate 4:3 and 3:4 cover images from artifact slide PDFs."""

from __future__ import annotations

import argparse
import site
import subprocess
import tempfile
import sys
from pathlib import Path

try:
    import fitz
    from PIL import Image, ImageColor, ImageEnhance, ImageFilter
except ModuleNotFoundError:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidate = Path(__file__).resolve().parent.parent / ".venv" / "lib" / version / "site-packages"
    if candidate.exists():
        site.addsitedir(str(candidate))
    import fitz
    from PIL import Image, ImageColor, ImageEnhance, ImageFilter


ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR = ROOT / "workspace" / "artifacts"
BACKGROUND_COLOR = ImageColor.getrgb("#1a1a2e")
RENDER_ZOOM = 3.0
SAFE_MARGIN = 0.10
BLURRED_BG_RATIO_FACTOR = 1.6
TARGET_SPECS = {
    "4x3": (1200, 900),
    "3x4": (900, 1200),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate 4:3 and 3:4 cover images from the first page of slide PDFs."
    )
    parser.add_argument(
        "--dir",
        dest="artifact_dir",
        type=Path,
        help="Specific artifact directory to process. Defaults to all artifact directories.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate cover files even if both outputs already exist.",
    )
    return parser.parse_args()


def resolve_artifact_dir(dir_arg: Path) -> Path:
    candidate = dir_arg.expanduser()
    if candidate.is_absolute():
        resolved = candidate
    else:
        direct = (Path.cwd() / candidate).resolve()
        resolved = direct if direct.exists() else (ARTIFACTS_DIR / candidate).resolve()

    if not resolved.exists() or not resolved.is_dir():
        raise FileNotFoundError(f"Artifact directory not found: {dir_arg}")
    return resolved


def discover_artifact_dirs(dir_arg: Path | None) -> list[Path]:
    if dir_arg is not None:
        return [resolve_artifact_dir(dir_arg)]
    if not ARTIFACTS_DIR.exists():
        return []
    artifact_dirs: list[Path] = []
    for path in ARTIFACTS_DIR.rglob("*"):
        if not path.is_dir():
            continue
        if any(path.glob("*_slides.pdf")) or any(path.glob("*_video.mp4")):
            artifact_dirs.append(path)
    return sorted(artifact_dirs)


def find_slides_pdf(artifact_dir: Path) -> Path | None:
    slides = sorted(artifact_dir.glob("*_slides.pdf"))
    return slides[0] if slides else None


def cover_paths(artifact_dir: Path, base_name: str) -> dict[str, Path]:
    return {
        "4x3": artifact_dir / f"{base_name}_cover_4x3.png",
        "3x4": artifact_dir / f"{base_name}_cover_3x4.png",
    }


def render_first_page(slides_pdf: Path) -> Image.Image:
    doc = fitz.open(slides_pdf)
    try:
        if doc.page_count < 1:
            raise ValueError("PDF has no pages")
        page = doc.load_page(0)
        pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    finally:
        doc.close()


def render_video_frame(video_path: Path, seconds: float = 0.2) -> Image.Image | None:
    """Render a single video frame as a PIL image."""
    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "frame.jpg"
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(seconds),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(out),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None

        if result.returncode != 0 or not out.is_file():
            return None
        return Image.open(out).convert("RGB")


def aspect_ratio(size: tuple[int, int]) -> float:
    width, height = size
    return width / height


def centered_crop_box(size: tuple[int, int], target_ratio: float) -> tuple[int, int, int, int]:
    src_width, src_height = size
    src_ratio = aspect_ratio(size)

    if abs(src_ratio - target_ratio) < 1e-6:
        return (0, 0, src_width, src_height)

    if src_ratio > target_ratio:
        crop_width = int(round(src_height * target_ratio))
        left = max(0, (src_width - crop_width) // 2)
        right = min(src_width, left + crop_width)
        return (left, 0, right, src_height)

    crop_height = int(round(src_width / target_ratio))
    top = max(0, (src_height - crop_height) // 2)
    bottom = min(src_height, top + crop_height)
    return (0, top, src_width, bottom)


def crop_within_safe_margin(size: tuple[int, int], crop_box: tuple[int, int, int, int]) -> bool:
    src_width, src_height = size
    left, top, right, bottom = crop_box
    return (
        left <= src_width * SAFE_MARGIN
        and (src_width - right) <= src_width * SAFE_MARGIN
        and top <= src_height * SAFE_MARGIN
        and (src_height - bottom) <= src_height * SAFE_MARGIN
    )


def resize_to_fit(image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    target_width, target_height = target_size
    scale = min(target_width / image.width, target_height / image.height)
    new_size = (
        max(1, int(round(image.width * scale))),
        max(1, int(round(image.height * scale))),
    )
    if new_size == image.size:
        return image.copy()
    return image.resize(new_size, Image.Resampling.LANCZOS)


def resize_to_fill(image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    target_width, target_height = target_size
    scale = max(target_width / image.width, target_height / image.height)
    new_size = (
        max(1, int(round(image.width * scale))),
        max(1, int(round(image.height * scale))),
    )
    resized = image.resize(new_size, Image.Resampling.LANCZOS)
    left = max(0, (resized.width - target_width) // 2)
    top = max(0, (resized.height - target_height) // 2)
    return resized.crop((left, top, left + target_width, top + target_height))


def paste_centered(background: Image.Image, foreground: Image.Image) -> Image.Image:
    x = (background.width - foreground.width) // 2
    y = (background.height - foreground.height) // 2
    background.paste(foreground, (x, y))
    return background


def compose_letterboxed_cover(source: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    canvas = Image.new("RGB", target_size, BACKGROUND_COLOR)
    foreground = resize_to_fit(source, target_size)
    return paste_centered(canvas, foreground)


def compose_blurred_cover(source: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    background = resize_to_fill(source, target_size)
    blur_radius = max(14, int(min(target_size) * 0.025))
    background = background.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    background = ImageEnhance.Brightness(background).enhance(0.42)
    background = Image.blend(background, Image.new("RGB", target_size, BACKGROUND_COLOR), 0.35)

    foreground = resize_to_fit(source, target_size)
    return paste_centered(background, foreground)


def build_cover_image(source: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    target_ratio = aspect_ratio(target_size)
    crop_box = centered_crop_box(source.size, target_ratio)

    if crop_within_safe_margin(source.size, crop_box):
        cropped = source.crop(crop_box)
        return cropped.resize(target_size, Image.Resampling.LANCZOS)

    src_ratio = aspect_ratio(source.size)
    ratio_factor = max(src_ratio / target_ratio, target_ratio / src_ratio)
    if ratio_factor >= BLURRED_BG_RATIO_FACTOR:
        return compose_blurred_cover(source, target_size)
    return compose_letterboxed_cover(source, target_size)


def save_covers(artifact_dir: Path, slides_pdf: Path) -> None:
    base_name = slides_pdf.stem.removesuffix("_slides")
    outputs = cover_paths(artifact_dir, base_name)
    first_page = render_first_page(slides_pdf)
    for label, size in TARGET_SPECS.items():
        image = build_cover_image(first_page, size)
        image.save(outputs[label], format="PNG", optimize=True)


def ensure_covers(artifact_dir: Path, force: bool = False) -> bool:
    """Generate or ensure 4:3 and 3:4 covers for a single artifact directory."""
    slides_pdf = find_slides_pdf(artifact_dir)
    video_path = artifact_dir / f"{artifact_dir.name}_video.mp4"
    if slides_pdf is None and not video_path.is_file():
        return False

    if slides_pdf is not None:
        source = render_first_page(slides_pdf)
        base_name = slides_pdf.stem.removesuffix("_slides")
    else:
        source = render_video_frame(video_path)
        if source is None:
            return False
        base_name = video_path.stem.removesuffix("_video")
    outputs = cover_paths(artifact_dir, base_name)
    if not force and all(path.exists() for path in outputs.values()):
        return True
    for label, size in TARGET_SPECS.items():
        image = build_cover_image(source, size)
        image.save(outputs[label], format="PNG", optimize=True)
    return True


def main() -> None:
    args = parse_args()
    try:
        artifact_dirs = discover_artifact_dirs(args.artifact_dir)
    except FileNotFoundError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1) from exc

    candidates: list[tuple[Path, Path | None]] = []
    for artifact_dir in artifact_dirs:
        slides_pdf = find_slides_pdf(artifact_dir)
        video_path = artifact_dir / f"{artifact_dir.name}_video.mp4"
        if slides_pdf is not None or video_path.is_file():
            candidates.append((artifact_dir, slides_pdf))
        elif args.artifact_dir is not None:
            print(f"Skipping {artifact_dir.name}: no *_slides.pdf or *_video.mp4 found")

    total = len(candidates)
    if total == 0:
        print("Generated covers for 0 artifacts, skipped 0 (already exist)")
        return

    generated = 0
    skipped_existing = 0
    errors = 0

    for index, (artifact_dir, slides_pdf) in enumerate(candidates, start=1):
        if slides_pdf is not None:
            base_name = slides_pdf.stem.removesuffix("_slides")
        else:
            base_name = f"{artifact_dir.name}"
        outputs = cover_paths(artifact_dir, base_name)

        if not args.force and all(path.exists() for path in outputs.values()):
            skipped_existing += 1
            print(f"Skipping {artifact_dir.name} ({index}/{total}): covers already exist")
            continue

        try:
            if ensure_covers(artifact_dir, force=args.force):
                generated += 1
                print(f"Generated covers for {artifact_dir.name} ({index}/{total})")
            else:
                errors += 1
                print(f"Error processing {artifact_dir.name} ({index}/{total}): no source media found")
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"Error processing {artifact_dir.name} ({index}/{total}): {exc}")
            continue

    print(f"Generated covers for {generated} artifacts, skipped {skipped_existing} (already exist)")
    if errors:
        print(f"Encountered errors in {errors} artifact(s)")


if __name__ == "__main__":
    main()
