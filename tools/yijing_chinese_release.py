#!/usr/bin/env python3
"""Generate Chinese audio for Yijing category articles, then publish SlideSync release."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioJob:
    slug: str
    source_md: Path
    artifact_dir: Path

    @property
    def audio_path(self) -> Path:
        return self.artifact_dir / f"{self.slug}_audio.wav"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
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
        "--audio-jobs",
        type=int,
        default=2,
        help="Parallel NotebookLM audio generation workers (default: 2)",
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


def main() -> int:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    source_dir = repo_root / "workspace" / "易经" / "generated"
    artifacts_dir = repo_root / "workspace" / "artifacts" / "categories" / "易经"
    slidesync_root = repo_root.parent / "SlideSync"
    release_root = repo_root / "workspace" / "artifacts" / "releases" / args.release_tag
    report_json = release_root / "audio_generation_report.json"
    report_md = release_root / "audio_generation_report.md"

    jobs = discover_jobs(source_dir, artifacts_dir, args.only)
    if not jobs:
        print("No matching Yijing category jobs found.")
        return 0

    release_root.mkdir(parents=True, exist_ok=True)
    print(f"Discovered {len(jobs)} Yijing category jobs.")

    records: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.audio_jobs)) as executor:
        future_map = {
            executor.submit(
                generate_and_verify_audio,
                repo_root=repo_root,
                slidesync_root=slidesync_root,
                job=job,
                language_model=args.audio_language_model,
            ): job
            for job in jobs
        }
        for future in as_completed(future_map):
            job = future_map[future]
            try:
                record = future.result()
            except BaseException as exc:
                record = {
                    "slug": job.slug,
                    "source_md": str(job.source_md),
                    "artifact_dir": str(job.artifact_dir),
                    "audio_path": str(job.audio_path),
                    "status": "error",
                    "language": "",
                    "confidence": 0.0,
                    "top3": [],
                    "detail": str(exc),
                }
            records.append(record)
            print(
                f"[audio] {record['slug']}: {record['status']}"
                f" lang={record['language'] or '-'} conf={record['confidence']}"
            )
            write_report(report_json, report_md, records, args.release_tag, args.release_title)

    success_count = sum(
        1
        for record in records
        if record["status"] in {"verified", "verified-existing"}
    )
    print(f"Verified zh audio items: {success_count}/{len(records)}")

    if success_count == 0:
        print(f"No zh audio verified. Report: {report_md}")
        return 1

    slidesync_command = [
        sys.executable,
        str(repo_root / "tools" / "slidesync_release_batch.py"),
        "--release-tag",
        args.release_tag,
        "--release-title",
        args.release_title,
        "--only",
        "categories/易经",
        "--audio-language",
        "zh",
        "--audio-language-model",
        args.audio_language_model,
        "--jobs",
        str(args.slidesync_jobs),
    ]
    print("Running SlideSync release batch...")
    run_command(slidesync_command, cwd=repo_root)
    print(f"Release ready: https://github.com/the-omega-institute/Omega-paper-series/releases/tag/{args.release_tag}")
    return 0


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


def generate_and_verify_audio(
    *,
    repo_root: Path,
    slidesync_root: Path,
    job: AudioJob,
    language_model: str,
) -> dict[str, object]:
    if job.audio_path.exists():
        detection = detect_audio_language(
            slidesync_root=slidesync_root,
            audio_path=job.audio_path,
            model_name=language_model,
        )
        if detection["language"] == "zh" and float(detection["confidence"]) >= 0.95:
            return {
                "slug": job.slug,
                "source_md": str(job.source_md),
                "artifact_dir": str(job.artifact_dir),
                "audio_path": str(job.audio_path),
                "status": "verified-existing",
                "language": detection["language"],
                "confidence": detection["confidence"],
                "top3": detection["top3"],
                "detail": "Existing audio already verified as zh.",
            }

    command = [
        sys.executable,
        "-u",
        str(repo_root / "tools" / "notebooklm_batch.py"),
        "--input",
        str(job.source_md),
        "--type",
        "audio",
        "--language-profile",
        "zh",
    ]
    generation = run_command(command, cwd=repo_root)
    detection = detect_audio_language(
        slidesync_root=slidesync_root,
        audio_path=job.audio_path,
        model_name=language_model,
    )

    status = "verified"
    detail = generation.stdout[-4000:] if generation.stdout else ""
    if detection["language"] != "zh" or float(detection["confidence"]) < 0.95:
        status = "wrong-language"

    return {
        "slug": job.slug,
        "source_md": str(job.source_md),
        "artifact_dir": str(job.artifact_dir),
        "audio_path": str(job.audio_path),
        "status": status,
        "language": detection["language"],
        "confidence": detection["confidence"],
        "top3": detection["top3"],
        "detail": detail,
    }


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


def write_report(
    report_json: Path,
    report_md: Path,
    records: list[dict[str, object]],
    release_tag: str,
    release_title: str,
) -> None:
    ordered = sorted(records, key=lambda record: str(record["slug"]))
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
        "| Slug | Status | Language | Confidence | Audio |",
        "|---|---|---:|---:|---|",
    ]
    for record in ordered:
        lines.append(
            f"| `{record['slug']}` | `{record['status']}` | `{record['language']}` | "
            f"`{float(record['confidence']):.6f}` | `{record['audio_path']}` |"
        )
    report_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=6 * 60 * 60,
        check=False,
    )
    if completed.returncode == 0:
        return completed
    detail = (completed.stderr or completed.stdout).strip()
    raise RuntimeError(f"Command failed: {' '.join(command)}\n{detail[:4000]}")


if __name__ == "__main__":
    raise SystemExit(main())
