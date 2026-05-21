#!/usr/bin/env python3
"""Batch-generate SlideSync reviewable videos and upload them to a GitHub release.

For every artifact directory that contains a complete `audio + slides` pair,
this script can:

1. Resolve the source markdown from `manifest.json`
2. Run SlideSync to build a reviewable synchronized video
3. Generate a uniform per-item description markdown
4. Upload `source.md + description.md + reviewable.mp4` to one GitHub release
5. Maintain a merged batch index for resume/re-run workflows
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RELEASE_REPO = "the-omega-institute/Omega-paper-series"
DEFAULT_RELEASE_TITLE = "SlideSync Reviewable Batch Pack"


@dataclass(frozen=True)
class BatchItem:
    slug: str
    relative_dir: str
    artifact_dir: Path
    manifest_path: Path
    source_md: Path
    audio_file: Path
    slides_pdf: Path
    title: str
    summary: str

    @property
    def source_asset_name(self) -> str:
        return f"{self.slug}.md"

    @property
    def description_asset_name(self) -> str:
        return f"{self.slug}_content_description.md"

    @property
    def video_asset_name(self) -> str:
        return f"{self.slug}_slidesync_reviewable.mp4"


@dataclass(frozen=True)
class LocalProcessResult:
    generation_record: dict[str, Any]
    asset_paths: tuple[Path, ...]


@dataclass(frozen=True)
class AudioLanguageRecord:
    language: str
    confidence: float
    top3: tuple[tuple[str, float], ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch build SlideSync release assets")
    parser.add_argument("--release-tag", required=True, help="GitHub release tag to create/update")
    parser.add_argument(
        "--release-title",
        default=DEFAULT_RELEASE_TITLE,
        help=f"GitHub release title (default: {DEFAULT_RELEASE_TITLE})",
    )
    parser.add_argument("--repo", default=RELEASE_REPO, help=f"GitHub repo (default: {RELEASE_REPO})")
    parser.add_argument("--whisper-model", default="base", help="Whisper model passed to SlideSync")
    parser.add_argument("--no-codex", action="store_true", help="Disable codex_cli alignment refinement")
    parser.add_argument("--limit", type=int, help="Process at most N items after filtering")
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Only process items whose slug or relative path contains this substring; repeatable",
    )
    parser.add_argument("--dry-run", action="store_true", help="Discover and stage metadata without running generation/upload")
    parser.add_argument("--force-generate", action="store_true", help="Rerun SlideSync even if draft.mp4 already exists")
    parser.add_argument("--force-upload", action="store_true", help="Re-upload assets with --clobber even if they already exist")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel SlideSync worker count (default: 1)")
    parser.add_argument(
        "--audio-language",
        choices=["zh", "en", "any"],
        default="zh",
        help="Only process items whose detected audio language matches this value (default: zh)",
    )
    parser.add_argument(
        "--audio-language-model",
        default="tiny",
        help="Whisper model used for preflight audio language detection (default: tiny)",
    )
    parser.add_argument(
        "--audio-language-min-confidence",
        type=float,
        default=0.95,
        help="Minimum confidence required for the audio language gate (default: 0.95)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    omega_root = Path(__file__).resolve().parents[1]
    slidesync_root = omega_root.parent / "SlideSync"
    artifacts_root = omega_root / "workspace" / "artifacts"
    release_root = artifacts_root / "releases" / args.release_tag
    release_files_root = release_root / "files"
    index_json_path = release_root / "slidesync_batch_index.json"
    index_md_path = release_root / "slidesync_batch_index.md"
    audio_inventory_json_path = release_root / "audio_language_inventory.json"
    audio_inventory_md_path = release_root / "audio_language_inventory.md"

    ensure_local_requirements(slidesync_root)
    release_files_root.mkdir(parents=True, exist_ok=True)

    items = discover_batch_items(omega_root, artifacts_root)
    items = filter_items(items, args.only, args.limit)
    audio_language_records: dict[str, AudioLanguageRecord] = {}
    if items and args.audio_language != "any":
        audio_language_records = detect_audio_languages(
            items=items,
            slidesync_root=slidesync_root,
            model_name=args.audio_language_model,
            inventory_json_path=audio_inventory_json_path,
            inventory_md_path=audio_inventory_md_path,
        )
        items = filter_items_by_audio_language(
            items,
            audio_language_records,
            required_language=args.audio_language,
            min_confidence=args.audio_language_min_confidence,
        )
    if not items:
        print("No matching audio+slides pairs found.")
        if args.audio_language != "any":
            print(
                "Audio language gate removed all candidates "
                f"(required={args.audio_language}, min_confidence={args.audio_language_min_confidence})."
            )
            print(f"Audio inventory report: {audio_inventory_md_path}")
        return 0

    print(f"Discovered {len(items)} items to process for release {args.release_tag}")

    existing_assets = set()
    if not args.dry_run:
        ensure_release_exists(args.repo, args.release_tag, args.release_title)
        existing_assets = list_release_assets(args.repo, args.release_tag)

    existing_index = load_existing_index(index_json_path)
    processed_records: dict[str, dict[str, Any]] = dict(existing_index)
    work_items: list[tuple[int, BatchItem]] = []
    for index, item in enumerate(items, start=1):
        print(f"[{index}/{len(items)}] {item.slug} :: {item.title}")
        item_assets = {
            item.source_asset_name,
            item.description_asset_name,
            item.video_asset_name,
        }
        if item_assets.issubset(existing_assets) and not args.force_upload and item.slug in processed_records:
            print("  release already contains all 3 assets; skipping")
            continue
        work_items.append((index, item))

    if args.jobs <= 1 or args.dry_run:
        for _, item in work_items:
            try:
                result = process_item_local(
                    omega_root=omega_root,
                    slidesync_root=slidesync_root,
                    release_files_root=release_files_root,
                    release_tag=args.release_tag,
                    item=item,
                    whisper_model=args.whisper_model,
                    no_codex=args.no_codex,
                    dry_run=args.dry_run,
                    force_generate=args.force_generate,
                )
            except KeyboardInterrupt:
                raise
            except BaseException as exc:
                print(f"  [ERROR] {item.slug}: {exc}", file=sys.stderr)
                record_item_error(
                    args=args,
                    item=item,
                    exc=exc,
                    processed_records=processed_records,
                    index_json_path=index_json_path,
                    index_md_path=index_md_path,
                )
                continue

            persist_successful_result(
                args=args,
                result=result,
                existing_assets=existing_assets,
                processed_records=processed_records,
                index_json_path=index_json_path,
                index_md_path=index_md_path,
            )
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
            future_map = {
                executor.submit(
                    process_item_local,
                    omega_root=omega_root,
                    slidesync_root=slidesync_root,
                    release_files_root=release_files_root,
                    release_tag=args.release_tag,
                    item=item,
                    whisper_model=args.whisper_model,
                    no_codex=args.no_codex,
                    dry_run=args.dry_run,
                    force_generate=args.force_generate,
                ): item
                for _, item in work_items
            }

            for future in as_completed(future_map):
                item = future_map[future]
                try:
                    result = future.result()
                except KeyboardInterrupt:
                    raise
                except BaseException as exc:
                    print(f"  [ERROR] {item.slug}: {exc}", file=sys.stderr)
                    record_item_error(
                        args=args,
                        item=item,
                        exc=exc,
                        processed_records=processed_records,
                        index_json_path=index_json_path,
                        index_md_path=index_md_path,
                    )
                    continue

                persist_successful_result(
                    args=args,
                    result=result,
                    existing_assets=existing_assets,
                    processed_records=processed_records,
                    index_json_path=index_json_path,
                    index_md_path=index_md_path,
                )

    if processed_records:
        write_index_files(index_json_path, index_md_path, processed_records, args.release_tag, args.release_title)
        if not args.dry_run:
            upload_release_assets(
                repo=args.repo,
                release_tag=args.release_tag,
                asset_paths=[index_json_path, index_md_path],
            )
            update_release_notes(
                repo=args.repo,
                release_tag=args.release_tag,
                release_title=args.release_title,
                notes_file=index_md_path,
            )

    print(f"Completed batch workflow for {len(processed_records)} items.")
    print(f"Release URL: https://github.com/{args.repo}/releases/tag/{args.release_tag}")
    return 0


def process_item_local(
    *,
    omega_root: Path,
    slidesync_root: Path,
    release_files_root: Path,
    release_tag: str,
    item: BatchItem,
    whisper_model: str,
    no_codex: bool,
    dry_run: bool,
    force_generate: bool,
) -> LocalProcessResult:
    item_stage = release_files_root / item.slug
    item_stage.mkdir(parents=True, exist_ok=True)

    source_asset_path = item_stage / item.source_asset_name
    description_asset_path = item_stage / item.description_asset_name
    video_asset_path = item_stage / item.video_asset_name

    sync_small_file(item.source_md, source_asset_path)

    generation_record: dict[str, Any] = {
        "slug": item.slug,
        "title": item.title,
        "relative_dir": item.relative_dir,
        "source_md": str(item.source_md),
        "audio_file": str(item.audio_file),
        "slides_pdf": str(item.slides_pdf),
        "source_asset_name": item.source_asset_name,
        "description_asset_name": item.description_asset_name,
        "video_asset_name": item.video_asset_name,
    }

    qa_payload: dict[str, Any] = {}
    timeline_payload: dict[str, Any] = {}

    if not dry_run:
        video_output_path, qa_payload, timeline_payload = ensure_slidesync_output(
            omega_root=omega_root,
            slidesync_root=slidesync_root,
            release_tag=release_tag,
            item=item,
            whisper_model=whisper_model,
            no_codex=no_codex,
            force_generate=force_generate,
        )
        link_large_file(video_output_path, video_asset_path)
        generation_record["generated_video"] = str(video_output_path)
        generation_record["qa_status"] = qa_payload.get("status", "unknown")
        generation_record["timeline_events"] = int(qa_payload.get("checks", {}).get("timelineEvents", 0))
        generation_record["warnings"] = list(qa_payload.get("warnings", []))
    else:
        generation_record["generated_video"] = ""
        generation_record["qa_status"] = "dry-run"
        generation_record["timeline_events"] = 0
        generation_record["warnings"] = []

    description_asset_path.write_text(
        build_description_markdown(item, qa_payload, timeline_payload),
        encoding="utf-8",
    )

    asset_paths: tuple[Path, ...]
    if dry_run:
        asset_paths = (source_asset_path, description_asset_path)
    else:
        asset_paths = (source_asset_path, description_asset_path, video_asset_path)
    return LocalProcessResult(generation_record=generation_record, asset_paths=asset_paths)


def persist_successful_result(
    *,
    args: argparse.Namespace,
    result: LocalProcessResult,
    existing_assets: set[str],
    processed_records: dict[str, dict[str, Any]],
    index_json_path: Path,
    index_md_path: Path,
) -> None:
    if not args.dry_run:
        upload_release_assets(
            repo=args.repo,
            release_tag=args.release_tag,
            asset_paths=list(result.asset_paths),
        )
        existing_assets.update(path.name for path in result.asset_paths)

    processed_records[str(result.generation_record["slug"])] = result.generation_record
    write_index_files(index_json_path, index_md_path, processed_records, args.release_tag, args.release_title)

    if not args.dry_run:
        upload_release_assets(
            repo=args.repo,
            release_tag=args.release_tag,
            asset_paths=[index_json_path, index_md_path],
        )
        update_release_notes(
            repo=args.repo,
            release_tag=args.release_tag,
            release_title=args.release_title,
            notes_file=index_md_path,
        )


def record_item_error(
    *,
    args: argparse.Namespace,
    item: BatchItem,
    exc: BaseException,
    processed_records: dict[str, dict[str, Any]],
    index_json_path: Path,
    index_md_path: Path,
) -> None:
    processed_records[item.slug] = {
        "slug": item.slug,
        "title": item.title,
        "relative_dir": item.relative_dir,
        "source_md": str(item.source_md),
        "audio_file": str(item.audio_file),
        "slides_pdf": str(item.slides_pdf),
        "source_asset_name": item.source_asset_name,
        "description_asset_name": item.description_asset_name,
        "video_asset_name": item.video_asset_name,
        "generated_video": "",
        "qa_status": "error",
        "timeline_events": 0,
        "warnings": [str(exc)],
    }
    write_index_files(index_json_path, index_md_path, processed_records, args.release_tag, args.release_title)
    if not args.dry_run:
        upload_release_assets(
            repo=args.repo,
            release_tag=args.release_tag,
            asset_paths=[index_json_path, index_md_path],
        )
        update_release_notes(
            repo=args.repo,
            release_tag=args.release_tag,
            release_title=args.release_title,
            notes_file=index_md_path,
        )


def ensure_local_requirements(slidesync_root: Path) -> None:
    required_paths = [
        slidesync_root,
        slidesync_root / ".venv" / "bin" / "python",
        slidesync_root / "schemas" / "alignment-result.schema.json",
    ]
    missing = [str(path) for path in required_paths if not path.exists()]
    if missing:
        raise SystemExit(f"Missing SlideSync runtime prerequisites: {missing}")

    for command in ("gh", "ffmpeg", "ffprobe", "pdftoppm", "pdftotext", "tesseract", "codex"):
        if shutil.which(command) is None:
            raise SystemExit(f"Required executable not found in PATH: {command}")


def discover_batch_items(omega_root: Path, artifacts_root: Path) -> list[BatchItem]:
    items: list[BatchItem] = []
    for directory in sorted(path for path in artifacts_root.rglob("*") if path.is_dir()):
        audio_file = next(directory.glob("*_audio.wav"), None)
        slides_pdf = next(directory.glob("*_slides.pdf"), None)
        if audio_file is None or slides_pdf is None:
            continue

        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            continue

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_ref = payload.get("source_article") or payload.get("source")
        if not source_ref:
            continue

        source_md = (omega_root / str(source_ref)).resolve()
        if not source_md.exists():
            print(f"  [WARN] missing source markdown for {directory.name}: {source_ref}")
            continue

        relative_dir = directory.relative_to(artifacts_root).as_posix()
        title = resolve_item_title(payload, source_md, directory.name)
        summary = extract_markdown_summary(source_md)

        items.append(
            BatchItem(
                slug=directory.name,
                relative_dir=relative_dir,
                artifact_dir=directory,
                manifest_path=manifest_path,
                source_md=source_md,
                audio_file=audio_file,
                slides_pdf=slides_pdf,
                title=title,
                summary=summary,
            )
        )
    return items


def filter_items(items: list[BatchItem], only_filters: list[str], limit: int | None) -> list[BatchItem]:
    if only_filters:
        lowered = [item.lower() for item in only_filters]
        items = [
            item
            for item in items
            if any(
                token in item.slug.lower() or token in item.relative_dir.lower()
                for token in lowered
            )
        ]
    if limit is not None:
        items = items[:limit]
    return items


def detect_audio_languages(
    *,
    items: list[BatchItem],
    slidesync_root: Path,
    model_name: str,
    inventory_json_path: Path,
    inventory_md_path: Path,
) -> dict[str, AudioLanguageRecord]:
    inventory_json_path.parent.mkdir(parents=True, exist_ok=True)
    detector_input = [
        {
            "slug": item.slug,
            "relative_dir": item.relative_dir,
            "audio_file": str(item.audio_file),
        }
        for item in items
    ]

    detector_script = """
import json
import sys
from collections import Counter
from pathlib import Path
import whisper

items = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
model = whisper.load_model(sys.argv[2])
rows = []
for item in items:
    audio_file = item['audio_file']
    mel = whisper.log_mel_spectrogram(whisper.pad_or_trim(whisper.load_audio(audio_file))).to(model.device)
    _, probs = model.detect_language(mel)
    best = max(probs, key=probs.get)
    top3 = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)[:3]
    rows.append({
        'slug': item['slug'],
        'relative_dir': item['relative_dir'],
        'audio_file': audio_file,
        'language': best,
        'confidence': float(probs[best]),
        'top3': [[lang, float(score)] for lang, score in top3],
    })
rows.sort(key=lambda row: (row['language'], -row['confidence'], row['relative_dir'], row['slug']))
counts = Counter(row['language'] for row in rows)
print(json.dumps({'model': sys.argv[2], 'counts': dict(counts), 'items': rows}, ensure_ascii=False))
""".strip()

    with tempfile.TemporaryDirectory(prefix="slidesync-audio-lang-") as temp_dir:
        input_json = Path(temp_dir) / "audio-files.json"
        input_json.write_text(json.dumps(detector_input, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [
                str(slidesync_root / ".venv" / "bin" / "python"),
                "-c",
                detector_script,
                str(input_json),
                model_name,
            ],
            cwd=str(slidesync_root),
            capture_output=True,
            text=True,
            timeout=6 * 60 * 60,
            check=False,
        )

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"Audio language detection failed:\n{detail[:4000]}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Audio language detection returned invalid JSON: {exc}") from exc

    inventory_json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    inventory_md_path.write_text(build_audio_inventory_markdown(payload), encoding="utf-8")

    counts = payload.get("counts", {})
    print(f"Audio language inventory ({model_name}): {counts}")

    records: dict[str, AudioLanguageRecord] = {}
    for row in payload.get("items", []):
        audio_file = str(row.get("audio_file", ""))
        if not audio_file:
            continue
        top3 = tuple((str(lang), float(score)) for lang, score in row.get("top3", []))
        records[audio_file] = AudioLanguageRecord(
            language=str(row.get("language", "")),
            confidence=float(row.get("confidence", 0.0)),
            top3=top3,
        )
    return records


def filter_items_by_audio_language(
    items: list[BatchItem],
    audio_language_records: dict[str, AudioLanguageRecord],
    *,
    required_language: str,
    min_confidence: float,
) -> list[BatchItem]:
    kept: list[BatchItem] = []
    excluded: list[tuple[BatchItem, AudioLanguageRecord | None]] = []
    for item in items:
        record = audio_language_records.get(str(item.audio_file))
        if record is None:
            excluded.append((item, None))
            continue
        if record.language == required_language and record.confidence >= min_confidence:
            kept.append(item)
            continue
        excluded.append((item, record))

    print(
        "Audio language gate kept "
        f"{len(kept)}/{len(items)} items "
        f"(required={required_language}, min_confidence={min_confidence})."
    )
    for item, record in excluded[:20]:
        if record is None:
            print(f"  excluded {item.slug}: no language record")
            continue
        top3 = ", ".join(f"{lang}:{score:.6f}" for lang, score in record.top3)
        print(
            f"  excluded {item.slug}: detected={record.language} "
            f"confidence={record.confidence:.6f} top3=[{top3}]"
        )
    if len(excluded) > 20:
        print(f"  ... {len(excluded) - 20} more excluded items")
    return kept


def build_audio_inventory_markdown(payload: dict[str, Any]) -> str:
    counts = payload.get("counts", {})
    lines = [
        "# Audio Language Inventory",
        "",
        f"- Whisper model: `{payload.get('model', '')}`",
        f"- Total audio files: `{len(payload.get('items', []))}`",
        "",
    ]
    for language, count in sorted(counts.items()):
        lines.append(f"- `{language}`: `{count}`")
    lines.extend(
        [
            "",
            "| Slug | Artifact Dir | Language | Confidence | Top3 |",
            "|---|---|---:|---:|---|",
        ]
    )
    for row in payload.get("items", []):
        top3 = ", ".join(f"{lang}:{score:.6f}" for lang, score in row.get("top3", []))
        lines.append(
            f"| `{row.get('slug', '')}` | `{row.get('relative_dir', '')}` | "
            f"`{row.get('language', '')}` | `{float(row.get('confidence', 0.0)):.6f}` | {escape_pipe(top3)} |"
        )
    return "\n".join(lines) + "\n"


def resolve_item_title(manifest_payload: dict[str, Any], source_md: Path, fallback: str) -> str:
    for key in ("title_zh", "title", "title_en", "name_zh", "name_pinyin", "id"):
        value = str(manifest_payload.get(key, "")).strip()
        if value:
            return value

    text = source_md.read_text(encoding="utf-8", errors="replace")
    frontmatter_title = extract_frontmatter_title(text)
    if frontmatter_title:
        return frontmatter_title

    for line in strip_frontmatter(text).splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            if heading:
                return heading
    return fallback


def extract_markdown_summary(source_md: Path, max_chars: int = 320) -> str:
    text = strip_frontmatter(source_md.read_text(encoding="utf-8", errors="replace"))
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and not line.startswith("#") and not line.startswith(">")]
    if not lines:
        return "Summary unavailable."

    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("- ") or line.startswith("## "):
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
        if len(" ".join(current)) >= 80:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))

    summary = next((paragraph for paragraph in paragraphs if len(paragraph) >= 40), lines[0])
    summary = re.sub(r"\s+", " ", summary).strip()
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


def extract_frontmatter_title(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    if end == -1:
        return ""
    frontmatter = text[4:end]
    match = re.search(r'^title:\s*"?(.+?)"?\s*$', frontmatter, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def ensure_release_exists(repo: str, release_tag: str, release_title: str) -> None:
    viewed = gh("release", "view", release_tag, "--repo", repo, check=False)
    if viewed.returncode == 0:
        update_release_notes(repo, release_tag, release_title, None)
        return

    notes = "SlideSync batch release in progress."
    created = gh(
        "release",
        "create",
        release_tag,
        "--repo",
        repo,
        "--title",
        release_title,
        "--notes",
        notes,
    )
    require_gh_success(created, f"create release {release_tag}")


def update_release_notes(repo: str, release_tag: str, release_title: str, notes_file: Path | None) -> None:
    args = [
        "release",
        "edit",
        release_tag,
        "--repo",
        repo,
        "--title",
        release_title,
    ]
    if notes_file is not None:
        args.extend(["--notes-file", str(notes_file)])
    edited = gh(*args)
    require_gh_success(edited, f"edit release {release_tag}")


def list_release_assets(repo: str, release_tag: str) -> set[str]:
    viewed = gh(
        "release",
        "view",
        release_tag,
        "--repo",
        repo,
        "--json",
        "assets",
        "--jq",
        ".assets[].name",
    )
    require_gh_success(viewed, f"list assets for {release_tag}")
    return {line.strip() for line in viewed.stdout.splitlines() if line.strip()}


def ensure_slidesync_output(
    *,
    omega_root: Path,
    slidesync_root: Path,
    release_tag: str,
    item: BatchItem,
    whisper_model: str,
    no_codex: bool,
    force_generate: bool,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_root = slidesync_root / "runs" / release_tag / item.slug
    input_dir = run_root / "inputCase"
    project_dir = run_root / "project"
    output_video = project_dir / "output" / "draft.mp4"
    qa_report = project_dir / "output" / "qa-report.json"
    timeline_report = project_dir / "work" / "timeline.draft.json"

    schema_dir = project_dir / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    sync_small_file(
        slidesync_root / "schemas" / "alignment-result.schema.json",
        schema_dir / "alignment-result.schema.json",
    )

    prepare_input_case(item, input_dir)

    if not force_generate and output_video.exists() and qa_report.exists() and timeline_report.exists():
        return (
            output_video,
            json.loads(qa_report.read_text(encoding="utf-8")),
            json.loads(timeline_report.read_text(encoding="utf-8")),
        )

    command = [
        str(slidesync_root / ".venv" / "bin" / "python"),
        "-m",
        "slidesync.cli",
        "generate",
        str(input_dir),
        "--project-dir",
        str(project_dir),
        "--whisper-model",
        whisper_model,
        "--json",
    ]
    if no_codex:
        command.append("--no-codex")

    env = dict(os.environ)
    env["PYTHONPATH"] = str(slidesync_root / "src")

    completed = subprocess.run(
        command,
        cwd=str(slidesync_root),
        capture_output=True,
        text=True,
        env=env,
        timeout=6 * 60 * 60,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"SlideSync generation failed for {item.slug}:\n{detail[:4000]}")

    if not output_video.exists() or not qa_report.exists() or not timeline_report.exists():
        raise SystemExit(f"SlideSync generation finished without expected outputs for {item.slug}")

    return (
        output_video,
        json.loads(qa_report.read_text(encoding="utf-8")),
        json.loads(timeline_report.read_text(encoding="utf-8")),
    )


def prepare_input_case(item: BatchItem, input_dir: Path) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    link_or_copy(item.source_md, input_dir / "content01.md")
    link_or_copy(item.slides_pdf, input_dir / "deck.pdf")
    link_or_copy(item.audio_file, input_dir / "narration.wav")


def build_description_markdown(
    item: BatchItem,
    qa_payload: dict[str, Any],
    timeline_payload: dict[str, Any],
) -> str:
    warnings = list(qa_payload.get("warnings", []))
    checks = qa_payload.get("checks", {})
    timeline_events = timeline_payload.get("events", [])
    warning_block = "\n".join(f"- {warning}" for warning in warnings) if warnings else "- 无"
    preview_events = "\n".join(
        f"- `{event['time']:.2f}s` → slide `{event['slide']}` ({event['source']}, conf `{event['confidence']}`)"
        for event in timeline_events[:12]
        if isinstance(event, dict)
    )
    if not preview_events:
        preview_events = "- 无"

    qa_status = qa_payload.get("status", "unknown")
    audio_duration = checks.get("audioDuration", "")
    video_duration = checks.get("videoDuration", "")
    timeline_event_count = checks.get("timelineEvents", len(timeline_events))

    return f"""# {item.title}

## 内容简介

{item.summary}

## 本次 Release 资产

- `{item.source_asset_name}`: 源文 markdown
- `{item.description_asset_name}`: 本说明文件
- `{item.video_asset_name}`: SlideSync 生成的可审核视频

## 资产来源

- 资产目录: `{item.relative_dir}`
- 源文路径: `{item.source_md}`
- 音频路径: `{item.audio_file}`
- Slides 路径: `{item.slides_pdf}`

## SlideSync 生成结果

- QA 状态: `{qa_status}`
- 音频时长: `{audio_duration}`
- 视频时长: `{video_duration}`
- 时间轴事件数: `{timeline_event_count}`

## 时间轴预览

{preview_events}

## 人工复核提示

{warning_block}
"""


def write_index_files(
    index_json_path: Path,
    index_md_path: Path,
    records: dict[str, dict[str, Any]],
    release_tag: str,
    release_title: str,
) -> None:
    ordered = dict(sorted(records.items(), key=lambda item: item[0]))
    index_json_path.parent.mkdir(parents=True, exist_ok=True)
    index_json_path.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# {release_title}",
        "",
        f"- Release tag: `{release_tag}`",
        f"- Item count: `{len(ordered)}`",
        "",
        "| Slug | Title | QA | Events | Assets |",
        "|---|---|---:|---:|---|",
    ]
    for slug, record in ordered.items():
        qa_status = str(record.get("qa_status", "unknown"))
        timeline_events = int(record.get("timeline_events", 0))
        assets = ", ".join(
            [
                f"`{record.get('source_asset_name', '')}`",
                f"`{record.get('description_asset_name', '')}`",
                f"`{record.get('video_asset_name', '')}`",
            ]
        )
        lines.append(
            f"| `{slug}` | {escape_pipe(str(record.get('title', slug)))} | `{qa_status}` | `{timeline_events}` | {assets} |"
        )
    index_md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_pipe(text: str) -> str:
    return text.replace("|", "\\|")


def load_existing_index(index_json_path: Path) -> dict[str, dict[str, Any]]:
    if not index_json_path.exists():
        return {}
    try:
        payload = json.loads(index_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


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


def link_or_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        target.symlink_to(source)
    except OSError:
        shutil.copy2(source, target)


def upload_release_assets(repo: str, release_tag: str, asset_paths: list[Path]) -> None:
    command = [
        "release",
        "upload",
        release_tag,
        "--repo",
        repo,
        "--clobber",
        *[str(path) for path in asset_paths],
    ]
    uploaded = gh(*command)
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
    raise SystemExit(f"GitHub CLI failed while trying to {action}:\n{detail[:4000]}")


if __name__ == "__main__":
    sys.exit(main())
