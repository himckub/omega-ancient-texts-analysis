#!/usr/bin/env python3
"""Generate Yijing experiment audios from markdown + slides sources and upload to a release."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from notebooklm import NotebookLMClient
except ModuleNotFoundError:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [
        Path(sys.executable).resolve().parent.parent / "lib" / version / "site-packages",
        Path(__file__).resolve().parent.parent / ".venv" / "lib" / version / "site-packages",
    ]
    for candidate in candidates:
        if candidate.exists():
            site.addsitedir(str(candidate))
    from notebooklm import NotebookLMClient


RELEASE_REPO = "the-omega-institute/Omega-paper-series"
DEFAULT_RELEASE_TAG = "notebooklm-yijing-audio-md-plus-slides-2026-05-06"
DEFAULT_RELEASE_TITLE = "Yijing NotebookLM Audio Experiment: Markdown + Slides Sources"
DEFAULT_SAMPLE_SLUGS = [
    "category_01_primal_creation_pure_states",
    "category_02_dynamic_change_cyclic_completion",
    "category_03_obstruction_danger_abysmal",
]
CLIENT_HTTP_TIMEOUT = 30.0
AUDIO_TIMEOUT = 3600
ARTIFACT_CREATE_TIMEOUT = 120
ARTIFACT_DOWNLOAD_TIMEOUT = 600
POLL_RPC_TIMEOUT = 60
POLL_RPC_RETRIES = 10
POLL_ERROR_SLEEP = 10
ARTIFACT_RETRIES = 3
QUOTA_ERROR_MARKERS = (
    "quota",
    "rate limit",
    "ratelimit",
    "too many requests",
    "429",
    "resource_exhausted",
    "resource exhausted",
    "user_displayable_error",
    # NotebookLM sometimes reports quota/backpressure as transient RPC failures
    # before audio generation starts, especially when creating many notebooks.
    "rpc create_notebook failed",
    "create_notebook failed",
    "rpc ccqfvf returned null result data",
    "possible server error or parameter mismatch",
    # Polling failures often mean the server-side task is still alive but the
    # client lost a stable RPC path; stop the batch instead of burning attempts.
    "audio polling failed repeatedly",
    "connection failed calling",
)
VIDEO_WORKER_LOG_NAME = "yijing_video_worker_2026-05-08.log"


class NotebookLMQuotaError(RuntimeError):
    """Raised when NotebookLM rejects artifact creation due to rate/quota limits."""


class NotebookLMGenerationError(RuntimeError):
    """Raised when NotebookLM rejects artifact creation for other reasons."""


@dataclass(frozen=True)
class ExperimentItem:
    slug: str
    source_md: Path
    slides_pdf: Path
    baseline_audio: Path | None
    title: str
    summary: str

    @property
    def experiment_audio_name(self) -> str:
        return f"{self.slug}_audio_md_plus_slides.wav"

    @property
    def baseline_audio_name(self) -> str:
        return f"{self.slug}_audio_baseline.wav"

    @property
    def slides_asset_name(self) -> str:
        return f"{self.slug}_slides.pdf"

    @property
    def source_asset_name(self) -> str:
        return f"{self.slug}.md"

    @property
    def report_asset_name(self) -> str:
        return f"{self.slug}_audio_experiment.md"

    @property
    def video_asset_name(self) -> str:
        return f"{self.slug}_slidesync_reviewable_manual_notebooklm.mp4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-tag", default=DEFAULT_RELEASE_TAG)
    parser.add_argument("--release-title", default=DEFAULT_RELEASE_TITLE)
    parser.add_argument("--repo", default=RELEASE_REPO)
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only process items whose slug contains this substring; repeatable",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum Yijing items to enumerate (default: 200)",
    )
    parser.add_argument(
        "--skip-video",
        action="store_true",
        help="Skip SlideSync video render; only generate + upload audio",
    )
    parser.add_argument(
        "--pause-between",
        type=float,
        default=0.0,
        help="Seconds to wait between successful items (default: 0)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    slidesync_root = repo_root.parent / "SlideSync"
    release_root = repo_root / "workspace" / "artifacts" / "releases" / args.release_tag
    release_files_root = release_root / "files"
    release_files_root.mkdir(parents=True, exist_ok=True)
    index_json_path = release_root / "audio_experiment_index.json"
    index_md_path = release_root / "audio_experiment_index.md"

    items = discover_items(repo_root, args.only, args.limit)
    if not items:
        print("No matching Yijing items with source markdown and slides were found.")
        return 1

    ensure_release_exists(args.repo, args.release_tag, args.release_title)

    records = load_existing_records(index_json_path)
    for item in items:
        existing = records.get(item.slug, {})
        existing_audio = existing.get("experiment_audio", "")
        expected_audio_path = release_files_root / item.slug / item.experiment_audio_name
        already_have_audio = (
            (existing_audio and Path(existing_audio).exists())
            or expected_audio_path.exists()
        )
        if already_have_audio:
            repaired = repair_existing_audio_record(
                item=item,
                existing=existing,
                expected_audio_path=expected_audio_path,
            )
            if repaired != existing:
                records[item.slug] = repaired
                write_index(index_json_path, index_md_path, records, args.release_tag, args.release_title)
            print(f"[audio-exp] skip {item.slug} :: experiment audio already on disk", flush=True)
            continue
        print(f"[audio-exp] {item.slug} :: {item.title}")
        result = asyncio.run(
            generate_one_experiment(
                item=item,
                repo_root=repo_root,
                slidesync_root=slidesync_root,
                release_files_root=release_files_root,
                release_tag=args.release_tag,
                skip_video=args.skip_video,
            )
        )
        records[item.slug] = result["record"]
        write_index(index_json_path, index_md_path, records, args.release_tag, args.release_title)
        asset_paths = result["asset_paths"] + [index_json_path, index_md_path]
        upload_release_assets(args.repo, args.release_tag, asset_paths)
        update_release_notes(args.repo, args.release_tag, args.release_title, index_md_path)
        if result["record"]["status"] == "success" and result["record"].get("experiment_audio"):
            trigger_video_worker(release_root, repo_root, item.slug)
        if result["record"]["status"] == "blocked_by_quota":
            print(
                f"  quota/rate limit on {item.slug}: pausing batch. "
                f"Restart later (failed slug auto-retries since audio absent).",
                flush=True,
            )
            break
        if result["record"]["status"] != "success":
            print(
                f"  failed {item.slug}: {result['record']['status']} :: "
                f"{result['record'].get('error', '')[:200]}; continuing",
                flush=True,
            )
            # transient NotebookLM failures shouldn't kill the batch;
            # the failed slug stays status=error in the index and gets
            # retried automatically next run (audio file absent → not skipped).
            continue
        if args.pause_between > 0:
            time.sleep(args.pause_between)

    print(f"Release URL: https://github.com/{args.repo}/releases/tag/{args.release_tag}")
    return 0


def load_existing_records(index_json_path: Path) -> dict[str, dict[str, Any]]:
    if not index_json_path.exists():
        return {}
    try:
        payload = json.loads(index_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def discover_items(repo_root: Path, only_filters: list[str], limit: int) -> list[ExperimentItem]:
    category_source_root = repo_root / "workspace" / "易经" / "generated"
    category_artifact_root = repo_root / "workspace" / "artifacts" / "categories" / "易经"
    hexagram_source_root = repo_root / "workspace" / "易经" / "hexagrams" / "all"
    hexagram_artifact_root = repo_root / "workspace" / "artifacts" / "易经"
    filters = [item.lower() for item in only_filters]
    items: list[ExperimentItem] = []

    sequence: list[tuple[str, Path, Path]] = []
    for path in sorted(category_source_root.glob("category_*.md")):
        sequence.append((path.stem, path, category_artifact_root / path.stem))
    for path in sorted(hexagram_source_root.glob("hexagram-*.md")):
        sequence.append((path.stem, path, hexagram_artifact_root / path.stem))

    for slug, source_md, artifact_dir in sequence:
        if filters and not any(token in slug.lower() for token in filters):
            continue
        slides_pdf = next(iter(sorted(artifact_dir.glob("*_slides.pdf"))), None)
        if not source_md.exists() or slides_pdf is None:
            continue
        baseline_audio = next(iter(sorted(artifact_dir.glob("*_audio.wav"))), None)
        title = resolve_item_title(source_md)
        summary = extract_markdown_summary(source_md)
        items.append(
            ExperimentItem(
                slug=slug,
                source_md=source_md,
                slides_pdf=slides_pdf,
                baseline_audio=baseline_audio,
                title=title,
                summary=summary,
            )
        )
        if len(items) >= limit:
            break
    return items


def resolve_item_title(source_md: Path) -> str:
    text = source_md.read_text(encoding="utf-8", errors="replace")
    for line in strip_frontmatter(text).splitlines():
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            if title:
                return title
    return source_md.stem


def extract_markdown_summary(source_md: Path, max_chars: int = 280) -> str:
    text = strip_frontmatter(source_md.read_text(encoding="utf-8", errors="replace"))
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#") and not line.startswith(">")]
    if not lines:
        return "Summary unavailable."
    summary = " ".join(lines[:8])
    summary = " ".join(summary.split())
    if len(summary) <= max_chars:
        return summary
    return summary[: max_chars - 1].rstrip() + "…"


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + 5 :]


async def generate_one_experiment(
    *,
    item: ExperimentItem,
    repo_root: Path,
    slidesync_root: Path,
    release_files_root: Path,
    release_tag: str,
    skip_video: bool = False,
) -> dict[str, Any]:
    item_stage = release_files_root / item.slug
    item_stage.mkdir(parents=True, exist_ok=True)

    source_asset = item_stage / item.source_asset_name
    slides_asset = item_stage / item.slides_asset_name
    experiment_audio_asset = item_stage / item.experiment_audio_name
    video_asset = item_stage / item.video_asset_name

    sync_small_file(item.source_md, source_asset)
    link_large_file(item.slides_pdf, slides_asset)

    notebook_id = ""
    text_source_id = ""
    slide_source_id = ""
    experiment_lang: dict[str, Any] | None = None
    experiment_duration = 0.0
    video_duration = 0.0
    status = "success"
    error_detail = ""

    try:
        async with await NotebookLMClient.from_storage(timeout=CLIENT_HTTP_TIMEOUT) as client:
            if not client.is_connected:
                await client.refresh_auth()
            if not client.is_connected:
                raise RuntimeError("NotebookLM is not connected. Run `notebooklm login` first.")

            notebook_title = f"Omega Audio Experiment: {item.slug}"
            notebook = await client.notebooks.create(title=notebook_title)
            notebook_id = notebook.id
            print(f"  notebook: [{notebook_id[:8]}] {notebook_title}", flush=True)

            text_content = build_text_source_content(item.source_md)
            print(f"  source[text]: {item.source_md.name}", flush=True)
            text_source = await client.sources.add_text(
                notebook_id,
                title=item.source_md.name,
                content=text_content,
                wait=True,
                wait_timeout=300.0,
            )
            text_source_id = text_source.id
            print(f"  source[text] ready: [{text_source_id[:8]}]", flush=True)

            print(f"  source[file]: {item.slides_pdf.name}", flush=True)
            slide_source = await client.sources.add_file(
                notebook_id,
                item.slides_pdf,
                wait=True,
                wait_timeout=900.0,
            )
            slide_source_id = slide_source.id
            print(f"  source[file] ready: [{slide_source_id[:8]}]", flush=True)
            notebook_sources = await client.sources.list(notebook_id)

            audio_status = None
            last_exc: Exception | None = None
            for attempt in range(1, ARTIFACT_RETRIES + 1):
                try:
                    print(f"  audio create attempt {attempt}/{ARTIFACT_RETRIES}", flush=True)
                    audio_status = await asyncio.wait_for(
                        client.artifacts.generate_audio(
                            notebook_id,
                            language="zh_Hans",
                            instructions=build_audio_instructions(item),
                        ),
                        timeout=ARTIFACT_CREATE_TIMEOUT,
                    )
                    handle_generation_status(audio_status)
                    print(f"  audio task: [{audio_status.task_id[:8]}]", flush=True)
                    break
                except NotebookLMQuotaError:
                    raise
                except Exception as exc:
                    last_exc = exc
                    print(f"  audio create failed: {type(exc).__name__}: {exc}", flush=True)
                    if attempt < ARTIFACT_RETRIES:
                        await asyncio.sleep(10)
            if audio_status is None:
                raise RuntimeError(
                    f"Failed to create audio artifact after {ARTIFACT_RETRIES} attempts: {last_exc}"
                )

            await wait_for_completion_resilient(
                client,
                notebook_id,
                audio_status.task_id,
                AUDIO_TIMEOUT,
                "audio",
            )
            print("  audio task completed", flush=True)
            await asyncio.wait_for(
                client.artifacts.download_audio(notebook_id, str(experiment_audio_asset)),
                timeout=ARTIFACT_DOWNLOAD_TIMEOUT,
            )
            print(f"  audio downloaded: {experiment_audio_asset.name}", flush=True)

        experiment_lang = detect_audio_language(slidesync_root, experiment_audio_asset)
        experiment_duration = probe_duration(experiment_audio_asset)
        if not skip_video:
            video_output = render_slidesync_video(
                slidesync_root=slidesync_root,
                release_tag=release_tag,
                item=item,
                source_md=source_asset,
                slides_pdf=slides_asset,
                audio_file=experiment_audio_asset,
            )
            sync_small_file(video_output, video_asset)
            video_duration = probe_duration(video_asset)
    except NotebookLMQuotaError as exc:
        status = "blocked_by_quota"
        error_detail = str(exc)
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
        status = "blocked_by_quota" if is_quota_error(error_detail) else "error"

    record = {
        "slug": item.slug,
        "title": item.title,
        "status": status,
        "error": error_detail,
        "notebook_id": notebook_id,
        "text_source_id": text_source_id,
        "slide_source_id": slide_source_id,
        "experiment_audio": str(experiment_audio_asset) if experiment_audio_asset.exists() else "",
        "experiment_language": experiment_lang["language"] if experiment_lang else "",
        "experiment_confidence": experiment_lang["confidence"] if experiment_lang else 0.0,
        "experiment_duration_seconds": experiment_duration,
        "video": str(video_asset) if video_asset.exists() else "",
        "video_duration_seconds": video_duration,
        "assets": [source_asset.name, slides_asset.name]
        + ([experiment_audio_asset.name] if experiment_audio_asset.exists() else [])
        + ([video_asset.name] if video_asset.exists() else []),
    }

    asset_paths = [source_asset, slides_asset]
    if experiment_audio_asset.exists():
        asset_paths.append(experiment_audio_asset)
    if video_asset.exists():
        asset_paths.append(video_asset)
    return {"record": record, "asset_paths": asset_paths}


def repair_existing_audio_record(
    *,
    item: ExperimentItem,
    existing: dict[str, Any],
    expected_audio_path: Path,
) -> dict[str, Any]:
    """Normalize stale error records when the audio file already exists.

    Concurrent watchdog/manual runs can race: one run may successfully write a
    WAV while an older run later writes an error record from its stale in-memory
    index. The WAV on disk is the source of truth for skip decisions.
    """
    audio_path = expected_audio_path
    existing_audio = existing.get("experiment_audio", "")
    if existing_audio and Path(existing_audio).exists():
        audio_path = Path(existing_audio)
    if not audio_path.exists():
        return existing

    repaired = dict(existing)
    changed = False

    def set_if_changed(key: str, value: Any) -> None:
        nonlocal changed
        if repaired.get(key) != value:
            repaired[key] = value
            changed = True

    set_if_changed("slug", item.slug)
    set_if_changed("title", item.title)
    set_if_changed("status", "success")
    set_if_changed("error", "")
    set_if_changed("experiment_audio", str(audio_path))

    if not repaired.get("experiment_duration_seconds"):
        try:
            set_if_changed("experiment_duration_seconds", probe_duration(audio_path))
        except Exception:
            pass

    assets = [item.source_asset_name, item.slides_asset_name, item.experiment_audio_name]
    if repaired.get("assets") != assets:
        set_if_changed("assets", assets)

    for key, default in [
        ("notebook_id", ""),
        ("text_source_id", ""),
        ("slide_source_id", ""),
        ("experiment_language", ""),
        ("experiment_confidence", 0.0),
        ("video", ""),
        ("video_duration_seconds", 0.0),
    ]:
        if key not in repaired:
            set_if_changed(key, default)

    return repaired if changed else existing


def trigger_video_worker(release_root: Path, repo_root: Path, slug: str) -> None:
    """Queue one audio-complete event and wake the short-lived video worker."""
    queue_path = release_root / "video_worker_events.jsonl"
    lock_path = release_root / "video_worker_events.lock"
    log_path = repo_root / "workspace" / "reports" / VIDEO_WORKER_LOG_NAME
    payload = {"slug": slug, "queued_at": time.time()}
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    import fcntl

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            with queue_path.open("a", encoding="utf-8") as queue:
                queue.write(json.dumps(payload, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(repo_root / "tools" / "yijing_video_worker.py"),
        "--event-queue",
    ]
    with log_path.open("a", encoding="utf-8") as log_file:
        subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    print(f"  video event queued: {slug}", flush=True)


def handle_generation_status(audio_status: Any) -> None:
    if getattr(audio_status, "status", "") == "failed":
        detail = " ".join(
            part
            for part in [
                str(getattr(audio_status, "error", "") or ""),
                str(getattr(audio_status, "error_code", "") or ""),
            ]
            if part
        ).strip()
        if is_quota_error(detail):
            raise NotebookLMQuotaError(detail or "NotebookLM quota/rate limit blocked audio creation")
        raise NotebookLMGenerationError(detail or "NotebookLM rejected audio creation")

    if not getattr(audio_status, "task_id", ""):
        raise RuntimeError("NotebookLM returned an empty audio task_id")


def build_text_source_content(source_md: Path) -> str:
    brief = (
        f"# 媒体生成说明\n\n"
        f"源文件: {source_md.name}\n\n"
        f"- 主语言: 中文\n"
        f"- 原文引文优先保留中文\n"
        f"- 定理名可保留英文\n"
    )
    content = source_md.read_text(encoding="utf-8")
    return f"{brief}\n\n---\n\n{content}"


def build_audio_instructions(item: ExperimentItem) -> str:
    return (
        "请基于源文 markdown 和已上传的 slides PDF 共同生成中文讲解音频。"
        "叙述顺序尽量跟随 slides 的页序推进，段落边界贴近页面切换。"
        "避免引入 slides 中完全没有体现的大段额外内容。"
        "整体语气保持清晰、克制、适合讲解型视频旁白。"
        f"主题标题：{item.title}"
    )


def is_quota_error(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in QUOTA_ERROR_MARKERS)


async def wait_for_completion_resilient(
    client: NotebookLMClient,
    notebook_id: str,
    task_id: str,
    timeout: float,
    artifact_name: str,
) -> Any:
    loop = asyncio.get_running_loop()
    start_time = loop.time()
    current_interval = 2.0
    consecutive_poll_errors = 0
    last_status = None

    while True:
        elapsed = loop.time() - start_time
        remaining = timeout - elapsed
        if remaining <= 0:
            raise TimeoutError(f"{artifact_name} task {task_id} timed out after {timeout}s")

        try:
            status = await asyncio.wait_for(
                client.artifacts.poll_status(notebook_id, task_id),
                timeout=min(POLL_RPC_TIMEOUT, max(1.0, remaining)),
            )
            consecutive_poll_errors = 0
        except Exception as exc:
            consecutive_poll_errors += 1
            if consecutive_poll_errors >= POLL_RPC_RETRIES:
                raise TimeoutError(
                    f"{artifact_name} polling failed repeatedly for task {task_id}: {exc}"
                ) from exc
            await asyncio.sleep(min(POLL_ERROR_SLEEP * consecutive_poll_errors, remaining))
            continue

        if status.is_complete:
            return status
        if status.is_failed:
            detail = status.error or status.error_code or status.status
            if is_quota_error(str(detail)):
                raise NotebookLMQuotaError(
                    f"{artifact_name} task {task_id} blocked by quota/rate limit: {detail}"
                )
            raise RuntimeError(f"{artifact_name} task {task_id} failed: {detail}")
        if status.status != last_status:
            last_status = status.status
            print(f"    status: {last_status}", flush=True)

        sleep_duration = min(current_interval, remaining)
        await asyncio.sleep(sleep_duration)
        current_interval = min(current_interval * 2, 10.0)


def detect_audio_language(slidesync_root: Path, audio_path: Path) -> dict[str, Any]:
    detector_script = """
import json
import sys
import whisper

audio_path = sys.argv[1]
model = whisper.load_model("tiny")
mel = whisper.log_mel_spectrogram(whisper.pad_or_trim(whisper.load_audio(audio_path))).to(model.device)
_, probs = model.detect_language(mel)
best = max(probs, key=probs.get)
top3 = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:3]
print(json.dumps({
    "language": best,
    "confidence": float(probs[best]),
    "top3": [[lang, float(score)] for lang, score in top3],
}, ensure_ascii=False))
""".strip()
    completed = subprocess.run(
        [
            str(slidesync_root / ".venv" / "bin" / "python"),
            "-c",
            detector_script,
            str(audio_path),
        ],
        cwd=str(slidesync_root),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"Audio language detection failed for {audio_path.name}: {detail[:2000]}")
    return json.loads(completed.stdout)


def probe_duration(path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        return 0.0
    try:
        return round(float(completed.stdout.strip()), 2)
    except ValueError:
        return 0.0


def render_slidesync_video(
    *,
    slidesync_root: Path,
    release_tag: str,
    item: ExperimentItem,
    source_md: Path,
    slides_pdf: Path,
    audio_file: Path,
) -> Path:
    run_root = slidesync_root / "runs" / release_tag / f"{item.slug}_manual"
    input_dir = run_root / "inputCase"
    project_dir = run_root / "project"
    output_video = project_dir / "output" / "draft.mp4"

    prepare_slidesync_input_case(
        input_dir=input_dir,
        source_md=source_md,
        slides_pdf=slides_pdf,
        audio_file=audio_file,
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(slidesync_root / "src")
    env["SLIDESYNC_DISABLE_SUBTITLES"] = "1"

    completed = subprocess.run(
        [
            str(slidesync_root / ".venv" / "bin" / "python"),
            "-m",
            "slidesync.cli",
            "generate",
            str(input_dir),
            "--project-dir",
            str(project_dir),
            "--json",
        ],
        cwd=str(slidesync_root),
        capture_output=True,
        text=True,
        timeout=6 * 60 * 60,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"SlideSync generation failed: {detail[:4000]}")
    if not output_video.exists():
        raise RuntimeError("SlideSync generation finished without draft.mp4")
    return output_video


def prepare_slidesync_input_case(
    *,
    input_dir: Path,
    source_md: Path,
    slides_pdf: Path,
    audio_file: Path,
) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    sync_small_file(source_md, input_dir / source_md.name)
    link_large_file(slides_pdf, input_dir / slides_pdf.name)
    link_large_file(audio_file, input_dir / audio_file.name)


def build_experiment_report(
    *,
    item: ExperimentItem,
    notebook_id: str,
    text_source_id: str,
    slide_source_id: str,
    notebook_sources: list[Any],
    experiment_audio_path: Path | None,
    experiment_lang: dict[str, Any] | None,
    experiment_duration: float,
    baseline_audio_path: Path | None,
    baseline_lang: dict[str, Any] | None,
    baseline_duration: float | None,
    video_path: Path | None,
    video_duration: float,
    status: str,
    error_detail: str,
) -> str:
    source_rows = "\n".join(
        f"- `{getattr(source, 'title', '')}` ({getattr(source, 'kind', '')})"
        for source in notebook_sources
    )
    if not source_rows:
        source_rows = "- 暂无 source 列表"

    baseline_block = "- 无 baseline 音频"
    if baseline_audio_path is not None and baseline_lang is not None:
        baseline_block = (
            f"- baseline 音频: `{baseline_audio_path.name}`\n"
            f"- baseline 语言检测: `{baseline_lang['language']}` / `{baseline_lang['confidence']:.6f}`\n"
            f"- baseline 时长: `{baseline_duration}` 秒"
        )

    experiment_block = "- 实验音频尚未生成"
    if experiment_audio_path is not None and experiment_lang is not None:
        experiment_block = (
            f"- 文件: `{experiment_audio_path.name}`\n"
            f"- 语言检测: `{experiment_lang['language']}` / `{experiment_lang['confidence']:.6f}`\n"
            f"- 时长: `{experiment_duration}` 秒"
        )

    video_block = "- 尚未生成视频"
    if video_path is not None:
        video_block = (
            f"- 文件: `{video_path.name}`\n"
            f"- 时长: `{video_duration}` 秒\n"
            f"- 字幕: `disabled`"
        )

    failure_block = ""
    if error_detail:
        failure_block = f"\n## 失败信息\n\n- 状态: `{status}`\n- 详情: `{error_detail}`\n"

    return f"""# {item.title}

## 实验目标

本次音频实验将 `源文 markdown + 已生成 slides PDF` 一起作为 NotebookLM source，
观察音频是否比单纯基于文案生成更贴近 slides 的页序与结构。

## 源输入

- markdown: `{item.source_md}`
- slides PDF: `{item.slides_pdf}`
- Notebook ID: `{notebook_id}`
- text source id: `{text_source_id}`
- slide source id: `{slide_source_id}`

## Notebook Source 列表

{source_rows}

## 新实验音频

{experiment_block}

## 合成视频

{video_block}

## Baseline 对照

{baseline_block}

## 运行状态

- 状态: `{status}`
{failure_block}

## 内容摘要

{item.summary}
"""


def write_index(
    index_json_path: Path,
    index_md_path: Path,
    records: dict[str, dict[str, Any]],
    release_tag: str,
    release_title: str,
) -> None:
    ordered = dict(sorted(records.items(), key=lambda item: item[0]))
    index_json_path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")

    categories = [s for s in ordered if s.startswith("category_")]
    hexagrams = [s for s in ordered if s.startswith("hexagram-")]
    cat_done = sum(1 for s in categories if (ordered[s].get("status") == "success") or ordered[s].get("experiment_audio"))
    hex_done = sum(1 for s in hexagrams if (ordered[s].get("status") == "success") or ordered[s].get("experiment_audio"))

    vid_done = sum(1 for s in ordered if ordered[s].get("video"))
    lines = [
        "# 易经 · 数学翻译 — 音频 + 视频",
        "",
        "把每一条易经源文 markdown + slides PDF 喂给 NotebookLM 生成中文讲解音频，",
        "再用 SlideSync (whisper + codex medium 字幕修订) 烧入字幕产出视频。",
        "",
        "## 跑批顺序",
        "",
        "1. 12 个 category（跨卦主题）",
        "2. 64 卦逐卦 hexagram-01 → hexagram-64",
        "",
        "音频 batch 与视频 worker 双线并行：音频出一条，视频 worker 接着合成一条。",
        "",
        "## 当前进度",
        "",
        f"- Audio: **{cat_done + hex_done} / 76**（category {cat_done}/12 · hexagram {hex_done}/64）",
        f"- Video: **{vid_done} / 76**",
        "",
        "## 每条产出（4 个文件）",
        "",
        "- `<slug>.md` — 源文",
        "- `<slug>_slides.pdf` — 幻灯片",
        "- `<slug>_audio_md_plus_slides.wav` — 中文讲解音频",
        "- `<slug>_video_burned.mp4` — 烧入字幕的视频（视频 worker 完成后上传）",
        "",
        "完整索引（NotebookLM 任务 ID、时长、置信度）见 `audio_experiment_index.json`。",
    ]
    index_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_pipe(text: str) -> str:
    return text.replace("|", "\\|")


def sync_small_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def link_large_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        try:
            target.symlink_to(source)
        except OSError:
            shutil.copy2(source, target)


def ensure_release_exists(repo: str, release_tag: str, release_title: str) -> None:
    viewed = gh("release", "view", release_tag, "--repo", repo, check=False)
    if viewed.returncode == 0:
        return
    created = gh(
        "release",
        "create",
        release_tag,
        "--repo",
        repo,
        "--title",
        release_title,
        "--notes",
        "Yijing audio experiment in progress.",
    )
    require_gh_success(created, f"create release {release_tag}")


def update_release_notes(repo: str, release_tag: str, release_title: str, notes_file: Path) -> None:
    edited = gh(
        "release",
        "edit",
        release_tag,
        "--repo",
        repo,
        "--title",
        release_title,
        "--notes-file",
        str(notes_file),
    )
    require_gh_success(edited, f"edit release {release_tag}")


def upload_release_assets(repo: str, release_tag: str, asset_paths: list[Path]) -> None:
    uploaded = gh(
        "release",
        "upload",
        release_tag,
        "--repo",
        repo,
        "--clobber",
        *[str(path) for path in asset_paths],
    )
    require_gh_success(uploaded, f"upload assets to {release_tag}")


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    if check:
        require_gh_success(completed, " ".join(args[:3]))
    return completed


def require_gh_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout).strip()
    raise RuntimeError(f"GitHub CLI failed while trying to {action}:\n{detail[:4000]}")


if __name__ == "__main__":
    raise SystemExit(main())
