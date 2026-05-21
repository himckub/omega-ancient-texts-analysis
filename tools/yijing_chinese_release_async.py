#!/usr/bin/env python3
"""Trigger Yijing category Chinese audio in parallel, recover outputs, then build SlideSync release."""

from __future__ import annotations

import argparse
import asyncio
import json
import site
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

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


ARTIFACT_CREATE_TIMEOUT = 120
ARTIFACT_DOWNLOAD_TIMEOUT = 600
POLL_RPC_TIMEOUT = 60
POLL_INTERVAL = 20
MAX_AUDIO_RETRIES = 2


@dataclass(frozen=True)
class AudioJob:
    slug: str
    source_md: Path
    artifact_dir: Path

    @property
    def audio_path(self) -> Path:
        return self.artifact_dir / f"{self.slug}_audio.wav"

    @property
    def manifest_path(self) -> Path:
        return self.artifact_dir / "manifest.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-dir",
        default="workspace/易经/generated",
        help="Source markdown directory, relative to repo root by default",
    )
    parser.add_argument(
        "--artifact-dir",
        default="workspace/artifacts/categories/易经",
        help="Artifact directory containing per-item folders, relative to repo root by default",
    )
    parser.add_argument(
        "--release-tag",
        default="slidesync-yijing-zh-2026-05-06",
        help="GitHub release tag for the SlideSync output",
    )
    parser.add_argument(
        "--release-title",
        default="Yijing Chinese SlideSync Reviewables",
        help="GitHub release title",
    )
    parser.add_argument(
        "--trigger-jobs",
        type=int,
        default=4,
        help="Parallel NotebookLM trigger workers (default: 4)",
    )
    parser.add_argument(
        "--slidesync-jobs",
        type=int,
        default=3,
        help="Parallel SlideSync workers (default: 3)",
    )
    parser.add_argument(
        "--audio-language-model",
        default="tiny",
        help="Whisper model used for audio language verification (default: tiny)",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only process Yijing category files whose slug contains this substring; repeatable",
    )
    return parser.parse_args()


def discover_jobs(source_dir: Path, artifacts_dir: Path, only_filters: list[str]) -> list[AudioJob]:
    filters = [item.lower() for item in only_filters]
    jobs: list[AudioJob] = []
    for source_md in sorted(source_dir.glob("*.md")):
        slug = source_md.stem
        if filters and not any(token in slug.lower() for token in filters):
            continue
        artifact_dir = artifacts_dir / slug
        if not artifact_dir.exists():
            continue
        slides_pdf = artifact_dir / f"{slug}_slides.pdf"
        if not slides_pdf.exists():
            continue
        jobs.append(AudioJob(slug=slug, source_md=source_md, artifact_dir=artifact_dir))
    return jobs


def load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def build_generation_brief(filepath: Path) -> str:
    return (
        f"# 媒体生成说明\n\n"
        f"源文件: {filepath.name}\n\n"
        f"- 主语言: 中文\n"
        f"- 原文引文优先保留中文\n"
        f"- 定理名可保留英文\n"
    )


async def resolve_notebook_id(client, job: AudioJob) -> str:
    manifest = load_manifest(job.manifest_path)
    existing_id = manifest.get("notebook_id")
    source_ref = manifest.get("source")
    same_source = source_ref == str(job.source_md)
    if existing_id and same_source:
        try:
            await asyncio.wait_for(client.notebooks.get(existing_id), timeout=30)
            return existing_id
        except Exception:
            pass

    title = f"Omega: {job.slug}"
    content = f"{build_generation_brief(job.source_md)}\n\n---\n\n{job.source_md.read_text(encoding='utf-8')}"
    nb = await asyncio.wait_for(client.notebooks.create(title=title), timeout=ARTIFACT_CREATE_TIMEOUT)
    await client.sources.add_text(nb.id, title=job.source_md.name, content=content, wait=True)
    await asyncio.sleep(3)
    manifest.update(
        {
            "source": str(job.source_md),
            "notebook_id": nb.id,
            "language_profile": "zh",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    )
    job.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return nb.id


def detect_audio_language(*, slidesync_root: Path, audio_path: Path, model_name: str) -> dict[str, object]:
    detector_script = """
import json
import sys
import whisper

audio_path = sys.argv[1]
model_name = sys.argv[2]
model = whisper.load_model(model_name)
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
            model_name,
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


def write_report(report_json: Path, report_md: Path, records: dict[str, dict[str, object]], release_tag: str, release_title: str) -> None:
    ordered = [records[key] for key in sorted(records)]
    report_json.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")
    verified_count = sum(
        1
        for record in ordered
        if record["status"] in {"verified", "verified-existing"}
    )
    lines = [
        f"# {release_title}",
        "",
        f"- Release tag: `{release_tag}`",
        f"- Audio jobs: `{len(ordered)}`",
        f"- Verified zh: `{verified_count}`",
        "",
        "| Slug | Status | Language | Confidence | Task | Audio |",
        "|---|---|---:|---:|---|---|",
    ]
    for record in ordered:
        lines.append(
            f"| `{record['slug']}` | `{record['status']}` | `{record.get('language', '')}` | "
            f"`{float(record.get('confidence', 0.0)):.6f}` | `{record.get('task_id', '')}` | `{record['audio_path']}` |"
        )
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def trigger_one(client, semaphore: asyncio.Semaphore, job: AudioJob, slidesync_root: Path, language_model: str) -> dict[str, object]:
    if job.audio_path.exists():
        detection = detect_audio_language(
            slidesync_root=slidesync_root,
            audio_path=job.audio_path,
            model_name=language_model,
        )
        if detection["language"] == "zh" and float(detection["confidence"]) >= 0.95:
            return {
                "slug": job.slug,
                "audio_path": str(job.audio_path),
                "status": "verified-existing",
                "language": detection["language"],
                "confidence": detection["confidence"],
                "top3": detection["top3"],
                "task_id": "",
                "notebook_id": load_manifest(job.manifest_path).get("notebook_id", ""),
                "retries": 0,
            }

    async with semaphore:
        notebook_id = await resolve_notebook_id(client, job)
        status = await asyncio.wait_for(
            client.artifacts.generate_audio(notebook_id, language="zh_Hans"),
            timeout=ARTIFACT_CREATE_TIMEOUT,
        )
        return {
            "slug": job.slug,
            "audio_path": str(job.audio_path),
            "status": "triggered",
            "language": "",
            "confidence": 0.0,
            "top3": [],
            "task_id": status.task_id,
            "notebook_id": notebook_id,
            "retries": 0,
        }


async def poll_and_recover(client, pending: dict[str, dict[str, object]], jobs_by_slug: dict[str, AudioJob], slidesync_root: Path, language_model: str) -> None:
    while True:
        active = [record for record in pending.values() if record["status"] == "triggered"]
        if not active:
            return

        for record in active:
            slug = str(record["slug"])
            job = jobs_by_slug[slug]
            try:
                status = await asyncio.wait_for(
                    client.artifacts.poll_status(str(record["notebook_id"]), str(record["task_id"])),
                    timeout=POLL_RPC_TIMEOUT,
                )
            except Exception:
                continue

            if getattr(status, "is_complete", False):
                await asyncio.wait_for(
                    client.artifacts.download_audio(str(record["notebook_id"]), str(job.audio_path)),
                    timeout=ARTIFACT_DOWNLOAD_TIMEOUT,
                )
                detection = detect_audio_language(
                    slidesync_root=slidesync_root,
                    audio_path=job.audio_path,
                    model_name=language_model,
                )
                record["language"] = detection["language"]
                record["confidence"] = detection["confidence"]
                record["top3"] = detection["top3"]
                record["status"] = (
                    "verified"
                    if detection["language"] == "zh" and float(detection["confidence"]) >= 0.95
                    else "wrong-language"
                )
            elif getattr(status, "is_failed", False):
                if int(record["retries"]) >= MAX_AUDIO_RETRIES:
                    record["status"] = "error"
                else:
                    retried = await asyncio.wait_for(
                        client.artifacts.generate_audio(str(record["notebook_id"]), language="zh_Hans"),
                        timeout=ARTIFACT_CREATE_TIMEOUT,
                    )
                    record["task_id"] = retried.task_id
                    record["retries"] = int(record["retries"]) + 1

        await asyncio.sleep(POLL_INTERVAL)


def run_slidesync_release(repo_root: Path, release_tag: str, release_title: str, slidesync_jobs: int, language_model: str) -> None:
    command = [
        sys.executable,
        str(repo_root / "tools" / "slidesync_release_batch.py"),
        "--release-tag",
        release_tag,
        "--release-title",
        release_title,
        "--only",
        "categories/易经",
        "--audio-language",
        "zh",
        "--audio-language-model",
        language_model,
        "--jobs",
        str(slidesync_jobs),
    ]
    completed = subprocess.run(
        command,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=6 * 60 * 60,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"SlideSync release batch failed:\n{detail[:4000]}")


async def main_async(args: argparse.Namespace) -> int:
    repo_root = Path(__file__).resolve().parents[1]
    source_dir = Path(args.source_dir)
    if not source_dir.is_absolute():
        source_dir = (repo_root / source_dir).resolve()
    artifacts_dir = Path(args.artifact_dir)
    if not artifacts_dir.is_absolute():
        artifacts_dir = (repo_root / artifacts_dir).resolve()
    slidesync_root = repo_root.parent / "SlideSync"
    release_root = repo_root / "workspace" / "artifacts" / "releases" / args.release_tag
    report_json = release_root / "audio_generation_report.json"
    report_md = release_root / "audio_generation_report.md"
    release_root.mkdir(parents=True, exist_ok=True)

    jobs = discover_jobs(source_dir, artifacts_dir, args.only)
    if not jobs:
        print("No matching Yijing category jobs found.")
        return 0

    print(f"Discovered {len(jobs)} Yijing category jobs.")
    jobs_by_slug = {job.slug: job for job in jobs}
    records: dict[str, dict[str, object]] = {}

    async with await NotebookLMClient.from_storage() as client:
        if not client.is_connected:
            await client.refresh_auth()
        if not client.is_connected:
            raise RuntimeError("NotebookLM is not connected. Run `notebooklm login` first.")

        semaphore = asyncio.Semaphore(max(1, args.trigger_jobs))
        tasks = [
            trigger_one(client, semaphore, job, slidesync_root, args.audio_language_model)
            for job in jobs
        ]
        for coro in asyncio.as_completed(tasks):
            record = await coro
            records[str(record["slug"])] = record
            print(
                f"[trigger] {record['slug']}: {record['status']}"
                f" task={record.get('task_id', '')[:8]}"
            )
            write_report(report_json, report_md, records, args.release_tag, args.release_title)

        await poll_and_recover(client, records, jobs_by_slug, slidesync_root, args.audio_language_model)
        write_report(report_json, report_md, records, args.release_tag, args.release_title)

    verified_count = sum(
        1
        for record in records.values()
        if record["status"] in {"verified", "verified-existing"}
    )
    print(f"Verified zh audio items: {verified_count}/{len(records)}")
    if verified_count == 0:
        print(f"No zh audio verified. Report: {report_md}")
        return 1

    print("Running SlideSync release batch...")
    run_slidesync_release(repo_root, args.release_tag, args.release_title, args.slidesync_jobs, args.audio_language_model)
    print(f"Release ready: https://github.com/the-omega-institute/Omega-paper-series/releases/tag/{args.release_tag}")
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
