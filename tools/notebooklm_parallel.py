#!/usr/bin/env python3
"""Parallel NotebookLM generation — fire all, recover later.

Instead of waiting for each video to complete (serial, 2hr/item),
this script:
  1. Creates notebooks + triggers ALL artifact types for N items in parallel
  2. Does NOT wait for video completion — just fires the task
  3. A separate recovery pass downloads whatever completed

Usage:
    # Trigger generation for all missing hexagrams (parallel, async)
    python tools/notebooklm_parallel.py --book 易经 --type chapter

    # Trigger for all missing content
    python tools/notebooklm_parallel.py

    # Recovery pass — download all server-completed videos
    python tools/notebooklm_parallel.py --recover

    # Limit concurrency (default 5)
    python tools/notebooklm_parallel.py --parallel 3
"""

import argparse
import asyncio
import json
import subprocess
import wave
import sys
import time
from pathlib import Path

try:
    from notebooklm import NotebookLMClient
except ModuleNotFoundError:
    venv = Path.home() / "venvs" / "notebooklm" / "lib"
    for sp in venv.rglob("site-packages"):
        sys.path.insert(0, str(sp))
    from notebooklm import NotebookLMClient

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT / "workspace"
ARTIFACTS = WORKSPACE / "artifacts"
REGISTRY = WORKSPACE / "publish_registry.json"
MIN_AUDIO_SECONDS = 0.20

# Timeouts for individual operations (not polling — just task creation)
CREATE_TIMEOUT = 120
SLIDES_POLL_TIMEOUT = 600
AUDIO_POLL_TIMEOUT = 600
# Video: fire and forget — don't poll, recover later
VIDEO_FIRE_TIMEOUT = 120  # just the create call, not completion


def load_registry():
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def find_source_md(entry):
    """Find source markdown for an entry."""
    article = entry.get("article")
    if article:
        p = WORKSPACE / article
        if p.is_file():
            return p
    # Search by ID
    item_id = entry["id"]
    for pattern in [f"*{item_id}*.md", f"*{item_id.replace('-', '_')}*.md"]:
        for src in WORKSPACE.rglob(pattern):
            if "generated" in str(src) or "hexagrams" in str(src) or "chapters" in str(src):
                return src
    return None


def find_artifact_dir(item_id):
    for d in ARTIFACTS.rglob(item_id):
        if d.is_dir():
            return d
    return None


def _ffprobe_json(path: Path) -> dict[str, object] | None:
    """Run ffprobe and return parsed JSON output."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_streams",
                "-show_format",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def _media_has_audio_track(path: Path) -> bool:
    """Whether an mp4 contains a non-empty audio stream."""
    data = _ffprobe_json(path)
    if not data or "streams" not in data:
        return False

    streams = data.get("streams") or []
    for stream in streams:
        if stream.get("codec_type") == "audio":
            raw_duration = stream.get("duration")
            duration = 0.0
            if raw_duration:
                try:
                    duration = float(raw_duration)
                except (TypeError, ValueError):
                    duration = 0.0
            if duration >= MIN_AUDIO_SECONDS:
                return True

            # Some muxed streams omit stream duration; fallback to container format.
            fmt = data.get("format") or {}
            raw_format_duration = fmt.get("duration")
            try:
                if raw_format_duration and float(raw_format_duration) >= MIN_AUDIO_SECONDS:
                    return True
            except (TypeError, ValueError):
                pass
    return False


def _audio_file_has_signal(path: Path) -> bool:
    if not path.exists():
        return False
    if path.stat().st_size < 1024:
        return False

    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wav:
                return wav.getnframes() > 0 and wav.getframerate() > 0
        except Exception:
            # Fall back to ffprobe for edge cases.
            pass

    data = _ffprobe_json(path)
    if not data:
        return False
    fmt = data.get("format") or {}
    duration = fmt.get("duration")
    if not duration:
        return False
    try:
        return float(duration) >= MIN_AUDIO_SECONDS
    except (TypeError, ValueError):
        return False


def _repair_video_with_audio(video_path: Path, audio_path: Path) -> bool:
    if not (video_path.exists() and audio_path.exists()):
        return False

    repaired = video_path.with_name(f"{video_path.stem}__with_audio{video_path.suffix}")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-shortest",
        "-movflags",
        "+faststart",
        str(repaired),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=300)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        print(f"    ⚠  ffmpeg remux failed for {video_path.name}: {result.stderr.strip().splitlines()[:2]}")
        if repaired.exists():
            repaired.unlink(missing_ok=True)
        return False

    repaired.replace(video_path)
    return True


def _run_cover_builder() -> bool:
    cmd = [sys.executable, str(REPO_ROOT / "tools" / "build_covers.py")]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1800)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        return False

    return True


def _run_cover_builder_forced() -> bool:
    cmd = [sys.executable, str(REPO_ROOT / "tools" / "build_covers.py"), "--force"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=1800)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    if result.returncode != 0:
        return False

    return True


def build_brief(language, filepath):
    if language == "en":
        return f"# Media Generation Brief\n\nSource: {filepath.name}\n\n- Primary: English\n"
    return (
        f"# 媒体生成说明\n\n源文件: {filepath.name}\n\n"
        f"- 主语言: 中文\n- 辅助: English (theorem names only)\n"
        f"- 原文引文保留中文\n"
    )


async def trigger_one(client, entry, semaphore, results):
    """Create notebook + trigger slides+audio+video for one item. Non-blocking."""
    item_id = entry["id"]
    language = entry.get("language", "zh")

    async with semaphore:
        source = find_source_md(entry)
        if not source:
            results.append({"id": item_id, "status": "skip", "reason": "no source md"})
            return

        artifact_dir = find_artifact_dir(item_id)
        if not artifact_dir:
            results.append({"id": item_id, "status": "skip", "reason": "no artifact dir"})
            return

        # Skip if already has video
        video = artifact_dir / f"{item_id}_video.mp4"
        if video.is_file() and video.stat().st_size > 10_000_000:
            results.append({"id": item_id, "status": "skip", "reason": "video exists"})
            return

        try:
            # Load or create notebook
            manifest_path = artifact_dir / "manifest.json"
            manifest = {}
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text())
                except json.JSONDecodeError:
                    pass

            nb_id = manifest.get("notebook_id")
            reused = False

            if nb_id:
                try:
                    await asyncio.wait_for(client.notebooks.get(nb_id), timeout=30)
                    reused = True
                except Exception:
                    nb_id = None

            if not nb_id:
                content = source.read_text(encoding="utf-8")
                brief = build_brief(language, source)
                full_content = f"{brief}\n\n---\n\n{content}"
                title = f"Omega: {item_id}"
                nb = await asyncio.wait_for(
                    client.notebooks.create(title=title), timeout=CREATE_TIMEOUT
                )
                nb_id = nb.id
                await client.sources.add_text(
                    nb_id, title=source.name, content=full_content, wait=True
                )
                await asyncio.sleep(3)

            slide_lang = "en" if language == "en" else "zh"
            tasks_fired = []

            # Trigger slides (if missing)
            slides_path = artifact_dir / f"{item_id}_slides.pdf"
            if not slides_path.is_file():
                try:
                    s = await asyncio.wait_for(
                        client.artifacts.generate_slide_deck(nb_id, language=slide_lang),
                        timeout=CREATE_TIMEOUT,
                    )
                    tasks_fired.append(f"slides:{s.task_id[:8]}")
                except Exception as e:
                    tasks_fired.append(f"slides:err:{e}")

            # Trigger audio (if missing)
            audio_path = artifact_dir / f"{item_id}_audio.wav"
            if not audio_path.is_file():
                try:
                    s = await asyncio.wait_for(
                        client.artifacts.generate_audio(nb_id),
                        timeout=CREATE_TIMEOUT,
                    )
                    tasks_fired.append(f"audio:{s.task_id[:8]}")
                except Exception as e:
                    tasks_fired.append(f"audio:err:{e}")

            # Trigger video — FIRE AND FORGET
            try:
                s = await asyncio.wait_for(
                    client.artifacts.generate_video(nb_id, language=slide_lang),
                    timeout=VIDEO_FIRE_TIMEOUT,
                )
                tasks_fired.append(f"video:{s.task_id[:8]}")
            except Exception as e:
                tasks_fired.append(f"video:err:{e}")

            # Update manifest
            manifest["notebook_id"] = nb_id
            manifest["source"] = str(source)
            manifest["language_profile"] = "zh_primary_bilingual" if language == "zh" else "en"
            manifest["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            status = "triggered" if any("err" not in t for t in tasks_fired) else "error"
            nb_label = f"reused:{nb_id[:8]}" if reused else f"new:{nb_id[:8]}"
            print(f"  ✓ {item_id} [{nb_label}] → {', '.join(tasks_fired)}")
            results.append({"id": item_id, "status": status, "tasks": tasks_fired})

        except Exception as e:
            print(f"  ✗ {item_id}: {type(e).__name__}: {str(e)[:60]}")
            results.append({"id": item_id, "status": "error", "error": str(e)[:100]})


async def recover_videos(client):
    """Download all server-completed videos that we don't have locally."""
    recovered = 0
    checked = 0
    fixed_audio = 0
    for mf in sorted(ARTIFACTS.rglob("manifest.json")):
        try:
            manifest = json.loads(mf.read_text())
        except:
            continue
        nb_id = manifest.get("notebook_id")
        if not nb_id:
            continue
        name = mf.parent.name
        if "_source" in name:
            continue

        video_path = mf.parent / f"{name}_video.mp4"
        audio_path = mf.parent / f"{name}_audio.wav"
        if video_path.is_file() and video_path.stat().st_size > 10_000_000:
            # 视频已存在也要做一遍音轨校验，避免上传静音文件
            if not _media_has_audio_track(video_path):
                if audio_path.exists() and _audio_file_has_signal(audio_path):
                    if _repair_video_with_audio(video_path, audio_path):
                        fixed_audio += 1
                        print(f"  ! {name}: repaired video by remuxing local audio")
                # 没有本地音频时，尝试再次拉取
                if not _media_has_audio_track(video_path):
                    try:
                        await client.artifacts.download_audio(nb_id, str(audio_path))
                        if _audio_file_has_signal(audio_path):
                            if _repair_video_with_audio(video_path, audio_path):
                                fixed_audio += 1
                                print(f"  ! {name}: repaired video by re-downloading audio")
                    except Exception:
                        pass
            checked += 1
            continue

        checked += 1
        try:
            raw = await client.artifacts._list_raw(nb_id)
            videos = [a for a in raw if isinstance(a, list) and len(a) > 4
                      and a[2] == 3 and a[4] == 3]  # VIDEO + COMPLETED
            if videos:
                await client.artifacts.download_video(nb_id, str(video_path))
                size_mb = video_path.stat().st_size / (1024 * 1024)
                print(f"  ✓ {name}: {size_mb:.1f}MB")
                recovered += 1
                if _media_has_audio_track(video_path):
                    pass
                else:
                    # 尝试优先用本地/最新下载的音频补齐视频
                    if not audio_path.exists() or not _audio_file_has_signal(audio_path):
                        try:
                            await client.artifacts.download_audio(nb_id, str(audio_path))
                        except Exception:
                            pass
                    if audio_path.exists() and _audio_file_has_signal(audio_path):
                        if _repair_video_with_audio(video_path, audio_path):
                            fixed_audio += 1
                            print(f"  ! {name}: repaired video by re-downloading audio")

            # Also grab slides + audio if missing
            slides_path = mf.parent / f"{name}_slides.pdf"
            if not slides_path.is_file():
                try:
                    await client.artifacts.download_slide_deck(nb_id, str(slides_path))
                    print(f"  ✓ {name}: slides recovered")
                except:
                    pass

            audio_path = mf.parent / f"{name}_audio.wav"
            if not audio_path.is_file():
                try:
                    await client.artifacts.download_audio(nb_id, str(audio_path))
                    print(f"  ✓ {name}: audio recovered")
                except:
                    pass

            if not _media_has_audio_track(video_path) and audio_path.exists() and _audio_file_has_signal(audio_path):
                if _repair_video_with_audio(video_path, audio_path):
                    fixed_audio += 1
                    print(f"  ! {name}: repaired video by muxing downloaded audio")

        except Exception:
            pass

        if checked % 20 == 0:
            print(f"  ... checked {checked}")

    print(f"\nChecked: {checked}, Recovered: {recovered}, Audio-stitched: {fixed_audio}")
    return recovered


async def main_async(args):
    async with await NotebookLMClient.from_storage(timeout=30.0) as client:
        if not client.is_connected:
            await client.refresh_auth()
        if not client.is_connected:
            print("NotebookLM 未连接。请运行: notebooklm login")
            sys.exit(1)
        print("NotebookLM connected ✓\n")

        if args.recover:
            await recover_videos(client)
            if not args.no_covers:
                if args.force_covers:
                    if not _run_cover_builder_forced():
                        print("  ⚠ cover generation failed during recover")
                else:
                    if not _run_cover_builder():
                        print("  ⚠ cover generation failed during recover")
            return

        registry = load_registry()
        items = [e for e in registry if not e.get("ready")]

        if args.book:
            items = [e for e in items if e.get("book") == args.book]
        if args.type:
            items = [e for e in items if e.get("type") == args.type]
        if args.language != "all":
            items = [e for e in items if e.get("language") == args.language]
        if args.max > 0:
            items = items[:args.max]

        print(f"Triggering {len(items)} items (parallel={args.parallel})\n")

        semaphore = asyncio.Semaphore(args.parallel)
        results = []
        tasks = [trigger_one(client, entry, semaphore, results) for entry in items]
        await asyncio.gather(*tasks)

        triggered = sum(1 for r in results if r["status"] == "triggered")
        skipped = sum(1 for r in results if r["status"] == "skip")
        errors = sum(1 for r in results if r["status"] == "error")
        print(f"\nDone: {triggered} triggered, {skipped} skipped, {errors} errors")
        print(f"\nRun with --recover in 30-60 min to download completed videos.")


def main():
    parser = argparse.ArgumentParser(description="Parallel NotebookLM generation")
    parser.add_argument("--book", help="Filter by book (e.g. 易经)")
    parser.add_argument("--type", help="Filter by type (chapter/category/synthesis/master)")
    parser.add_argument("--parallel", type=int, default=5, help="Max concurrent triggers")
    parser.add_argument("--max", type=int, default=0, help="Max items (0=all)")
    parser.add_argument("--recover", action="store_true", help="Recovery mode: download completed videos")
    parser.add_argument(
        "--language",
        choices=["zh", "en", "all"],
        default="zh",
        help="Filter registry items by language (default: zh)."
    )
    parser.add_argument(
        "--no-covers",
        action="store_true",
        help="Skip automatic cover regeneration during recover."
    )
    parser.add_argument(
        "--force-covers",
        action="store_true",
        help="Regenerate covers even if both ratio versions already exist."
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
