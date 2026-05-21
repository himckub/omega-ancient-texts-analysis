#!/usr/bin/env python3
"""Build SlideSync videos after NotebookLM audio finishes.

Event-driven mode is the default: the audio pipeline appends completed slugs to
video_worker_events.jsonl and starts this worker. The worker drains queued
events, builds videos for the ready audio files, then exits. A legacy scan loop
is still available with --scan-loop for manual recovery.

For each item with audio on disk and no video uploaded yet:
  1. Stage inputCase under SlideSync/runs/<slug>/inputCase
  2. slidesync generate (whisper + codex alignment, no burn)
  3. codex medium transcript correction against source md
  4. slidesync render with burned subtitles
  5. upload <slug>_video_burned.mp4 to the release

Uses a sidecar JSON to track state.
"""

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SLIDESYNC_ROOT = REPO_ROOT.parent / "SlideSync"
SLIDESYNC_VENV_PY = SLIDESYNC_ROOT / ".venv" / "bin" / "python"
RELEASE_DIR = REPO_ROOT / "workspace" / "artifacts" / "releases" / "notebooklm-yijing-audio-md-plus-slides-2026-05-06"
INDEX_JSON = RELEASE_DIR / "audio_experiment_index.json"
STATE_JSON = RELEASE_DIR / "video_worker_state.json"
EVENT_QUEUE_JSONL = RELEASE_DIR / "video_worker_events.jsonl"
EVENT_QUEUE_LOCK = RELEASE_DIR / "video_worker_events.lock"
WORKER_LOCK = RELEASE_DIR / "video_worker.lock"
RELEASE_TAG = "notebooklm-yijing-audio-md-plus-slides-2026-05-06"
RELEASE_REPO = "the-omega-institute/Omega-paper-series"

CATEGORY_SOURCE_DIR = REPO_ROOT / "workspace" / "易经" / "generated"
CATEGORY_ARTIFACT_DIR = REPO_ROOT / "workspace" / "artifacts" / "categories" / "易经"
HEXAGRAM_SOURCE_DIR = REPO_ROOT / "workspace" / "易经" / "hexagrams" / "all"
HEXAGRAM_ARTIFACT_DIR = REPO_ROOT / "workspace" / "artifacts" / "易经"

CODEX_TIMEOUT = 900
WHISPER_MODEL = "base"
WHISPER_LANG = "zh"


def log(msg: str) -> None:
    print(f"[video-worker {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_JSON.exists():
        try:
            return json.loads(STATE_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


@contextlib.contextmanager
def exclusive_worker_lock():
    """Allow only one video worker to mutate SlideSync runs/state at a time."""
    WORKER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with WORKER_LOCK.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(f"{os.getpid()}\n")
            lock_file.flush()
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_index() -> dict:
    if not INDEX_JSON.exists():
        log(f"index missing: {INDEX_JSON}")
        return {}
    try:
        payload = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log("index parse error")
        return {}
    return payload if isinstance(payload, dict) else {}


def drain_event_queue() -> list[str]:
    """Atomically read and clear queued video events, preserving first-seen order."""
    EVENT_QUEUE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_QUEUE_LOCK.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if not EVENT_QUEUE_JSONL.exists():
                return []
            lines = EVENT_QUEUE_JSONL.read_text(encoding="utf-8").splitlines()
            EVENT_QUEUE_JSONL.write_text("", encoding="utf-8")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    slugs: list[str] = []
    seen: set[str] = set()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        slug = ""
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                slug = str(payload.get("slug", "")).strip()
        except json.JSONDecodeError:
            slug = raw
        if slug and slug not in seen:
            seen.add(slug)
            slugs.append(slug)
    return slugs


def requeue_events(slugs: list[str]) -> None:
    if not slugs:
        return
    EVENT_QUEUE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with EVENT_QUEUE_LOCK.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            EVENT_QUEUE_JSONL.parent.mkdir(parents=True, exist_ok=True)
            with EVENT_QUEUE_JSONL.open("a", encoding="utf-8") as queue:
                for slug in slugs:
                    queue.write(json.dumps({"slug": slug, "requeued_at": time.time()}, ensure_ascii=False) + "\n")
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def resolve_inputs(slug: str) -> tuple[Path, Path] | None:
    """Return (source_md, slides_pdf) for slug, or None if missing."""
    if slug.startswith("category_"):
        md = CATEGORY_SOURCE_DIR / f"{slug}.md"
        pdf_dir = CATEGORY_ARTIFACT_DIR / slug
    elif slug.startswith("hexagram-"):
        md = HEXAGRAM_SOURCE_DIR / f"{slug}.md"
        pdf_dir = HEXAGRAM_ARTIFACT_DIR / slug
    else:
        return None
    if not md.exists():
        return None
    pdfs = sorted(pdf_dir.glob("*_slides.pdf"))
    if not pdfs:
        return None
    return md, pdfs[0]


def stage_input_case(*, slug: str, source_md: Path, slides_pdf: Path, audio_wav: Path) -> Path:
    input_dir = SLIDESYNC_ROOT / "runs" / "yijing-video-worker" / slug / "inputCase"
    if input_dir.exists():
        shutil.rmtree(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_md, input_dir / source_md.name)
    shutil.copy2(slides_pdf, input_dir / slides_pdf.name)
    shutil.copy2(audio_wav, input_dir / audio_wav.name)
    return input_dir


def run_generate(input_dir: Path, project_dir: Path) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    # SlideSync's codex_cli judge looks for schemas/alignment-result.schema.json
    # under project_dir; symlink the SlideSync repo's schemas/ so codex can run.
    schemas_link = project_dir / "schemas"
    if not schemas_link.exists():
        try:
            schemas_link.symlink_to(SLIDESYNC_ROOT / "schemas")
        except FileExistsError:
            pass
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SLIDESYNC_ROOT / "src")
    cmd = [
        str(SLIDESYNC_VENV_PY),
        "-m",
        "slidesync.cli",
        "generate",
        str(input_dir),
        "--project-dir",
        str(project_dir),
        "--whisper-model",
        WHISPER_MODEL,
        "--whisper-language",
        WHISPER_LANG,
        "--llm-provider",
        "codex_cli",
        "--no-burn-subtitles",
        "--json",
    ]
    log(f"  generate: {' '.join(cmd[2:])}")
    completed = subprocess.run(cmd, cwd=str(SLIDESYNC_ROOT), env=env, capture_output=True, text=True, timeout=3600)
    if completed.returncode != 0:
        raise RuntimeError(f"generate failed rc={completed.returncode}: {(completed.stderr or completed.stdout)[:2000]}")
    # Hard guarantee: codex must have judged the alignment, not silently fallen back.
    qa_report = project_dir / "output" / "qa-report.json"
    if qa_report.exists():
        try:
            payload = json.loads(qa_report.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        warnings = payload.get("warnings", []) if isinstance(payload, dict) else []
        for w in warnings:
            if "LLM judge unavailable" in str(w) or "coverage fallback" in str(w):
                raise RuntimeError(
                    f"codex judge did not run for this item; aborting (warning: {str(w)[:300]})"
                )


def run_codex_correction(*, project_dir: Path, source_md: Path) -> int:
    """Run codex medium against transcript.reviewed.json. Returns count applied."""
    transcript_path = project_dir / "work" / "transcript.reviewed.json"
    if not transcript_path.exists():
        log(f"  WARN: transcript.reviewed.json missing, skipping correction")
        return 0
    data = json.loads(transcript_path.read_text(encoding="utf-8"))
    segs = data["segments"]
    md_text = source_md.read_text(encoding="utf-8")
    lines = "\n".join(f"[{i}] {s['text']}" for i, s in enumerate(segs))
    prompt = f"""你是一名 ASR 字幕校对员。下面有两份输入：

【源文 markdown — 权威内容来源】
{md_text}

【ASR 字幕 — 每行格式 [行号] 文本】
{lines}

任务：
- 找出 ASR 转写错误（同音字、错别字、专有名词错写等），重点是中文术语和人名/书名。
- 只在有明确证据的情况下修改：源文里能找到正确写法、上下文/语义匹配。
- 不要改动语义正确的口语化表述，不要重写，不要添词删词。
- 标点不动。

输出严格 JSON（不要包 markdown fence），格式：
{{"corrections":[{{"idx": <int>, "old": "<原文片段>", "new": "<修正片段>", "reason": "<一句话理由>"}}, ...]}}

idx 是行号；old 必须是该行里能精确匹配到的子串；new 是替换后的子串。如果没有需要改的，返回 {{"corrections":[]}}。
"""
    completed = subprocess.run(
        [
            "codex",
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "-c",
            'model_reasoning_effort="medium"',
            prompt,
        ],
        capture_output=True,
        text=True,
        timeout=CODEX_TIMEOUT,
    )
    if completed.returncode != 0:
        log(f"  codex correction failed rc={completed.returncode}: {(completed.stderr or completed.stdout)[:500]}")
        return 0
    final_text = None
    for raw in completed.stdout.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "item.completed":
            item = evt.get("item", {})
            if item.get("type") == "agent_message":
                final_text = item.get("text", "")
    if not final_text:
        log("  codex: no agent_message")
        return 0
    final_text = final_text.strip()
    if final_text.startswith("```"):
        final_text = final_text.split("\n", 1)[1] if "\n" in final_text else ""
        if final_text.endswith("```"):
            final_text = final_text.rsplit("```", 1)[0]
        final_text = final_text.strip()
    try:
        payload = json.loads(final_text)
    except json.JSONDecodeError:
        log(f"  codex output not JSON: {final_text[:200]}")
        return 0
    corrections = payload.get("corrections", [])
    applied = 0
    for c in corrections:
        idx = c.get("idx")
        old = c.get("old", "")
        new = c.get("new", "")
        if isinstance(idx, int) and 0 <= idx < len(segs) and old in segs[idx]["text"]:
            segs[idx]["text"] = segs[idx]["text"].replace(old, new)
            applied += 1
    transcript_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    srt = project_dir / "output" / "subtitles.srt"
    if srt.exists() and applied:
        srt_text = srt.read_text(encoding="utf-8")
        for c in corrections:
            if c.get("old") and c.get("old") in srt_text:
                srt_text = srt_text.replace(c["old"], c["new"])
        srt.write_text(srt_text, encoding="utf-8")
    log(f"  codex applied {applied}/{len(corrections)} corrections")
    return applied


def run_render_burned(input_dir: Path, project_dir: Path) -> Path:
    output_name = "final_burned.mp4"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SLIDESYNC_ROOT / "src")
    cmd = [
        str(SLIDESYNC_VENV_PY),
        "-m",
        "slidesync.cli",
        "render",
        str(input_dir),
        "--project-dir",
        str(project_dir),
        "--output",
        output_name,
        "--json",
    ]
    log(f"  render: {' '.join(cmd[2:])}")
    completed = subprocess.run(cmd, cwd=str(SLIDESYNC_ROOT), env=env, capture_output=True, text=True, timeout=1800)
    if completed.returncode != 0:
        raise RuntimeError(f"render failed rc={completed.returncode}: {(completed.stderr or completed.stdout)[:2000]}")
    out = project_dir / "output" / output_name
    if not out.exists():
        raise RuntimeError(f"render finished but {out} missing")
    return out


def upload_to_release(slug: str, video_path: Path) -> None:
    target_video = video_path.parent / f"{slug}_video_burned.mp4"
    if target_video != video_path:
        shutil.copy2(video_path, target_video)
    log(f"  upload: {target_video.name}")
    completed = subprocess.run(
        ["gh", "release", "upload", RELEASE_TAG, "--repo", RELEASE_REPO, "--clobber", str(target_video)],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"upload failed: {(completed.stderr or completed.stdout)[:1000]}")


def process_one(slug: str, audio_wav: Path) -> dict:
    record = {"slug": slug, "started": time.time()}
    inputs = resolve_inputs(slug)
    if inputs is None:
        return {**record, "status": "skipped", "reason": "missing source md or slides pdf"}
    source_md, slides_pdf = inputs
    project_dir = SLIDESYNC_ROOT / "runs" / "yijing-video-worker" / slug
    input_dir = stage_input_case(slug=slug, source_md=source_md, slides_pdf=slides_pdf, audio_wav=audio_wav)
    try:
        run_generate(input_dir, project_dir)
        applied = run_codex_correction(project_dir=project_dir, source_md=source_md)
        video_path = run_render_burned(input_dir, project_dir)
        upload_to_release(slug, video_path)
    except Exception as exc:
        record.update({"status": "error", "error": str(exc)[:1000]})
        log(f"  ERROR {slug}: {exc}")
        return record
    record.update({"status": "success", "video": str(video_path), "corrections_applied": applied, "finished": time.time()})
    return record


def process_ready(*, slugs: list[str] | None, only_filters: list[str]) -> tuple[int, list[str]]:
    """Process ready items. Returns (processed_count, slugs_to_retry_later)."""
    state = load_state()
    index = load_index()
    retry_later: list[str] = []
    processed = 0

    candidates = slugs if slugs is not None else sorted(index.keys())
    lowered_filters = [item.lower() for item in only_filters]
    for slug in candidates:
        if lowered_filters and not any(token in slug.lower() for token in lowered_filters):
            continue
        rec = index.get(slug)
        if not isinstance(rec, dict):
            retry_later.append(slug)
            continue
        audio_str = rec.get("experiment_audio", "")
        if not audio_str:
            retry_later.append(slug)
            continue
        audio_wav = Path(audio_str)
        if not audio_wav.exists():
            retry_later.append(slug)
            continue
        if state.get(slug, {}).get("status") == "success":
            log(f"skip {slug}: video already marked success")
            continue
        log(f"=== {slug} ===")
        result = process_one(slug, audio_wav)
        state[slug] = result
        save_state(state)
        processed += 1
        if result["status"] == "success":
            log(f"  done: {slug} ({result.get('corrections_applied',0)} subtitle fixes)")
        else:
            log(f"  failed: {result.get('error','?')}")

    return processed, retry_later


def run_event_queue(idle_exit_delay: float) -> int:
    log(f"event worker starting; release={RELEASE_TAG}")
    idle_since: float | None = None
    total_processed = 0
    while True:
        slugs = drain_event_queue()
        if slugs:
            idle_since = None
            log(f"drained {len(slugs)} video event(s): {', '.join(slugs[:8])}")
            processed, retry_later = process_ready(slugs=slugs, only_filters=[])
            total_processed += processed
            if retry_later:
                log(f"drop not-ready event(s): {', '.join(retry_later[:8])}")
            continue

        if idle_since is None:
            idle_since = time.time()
        if time.time() - idle_since >= idle_exit_delay:
            log(f"event queue empty; exiting after processing {total_processed} item(s)")
            return 0
        time.sleep(1.0)


def run_scan_loop(args: argparse.Namespace) -> int:
    log(f"legacy scan-loop starting; release={RELEASE_TAG}")
    while True:
        process_ready(slugs=args.slug or None, only_filters=args.only)
        if args.once:
            return 0
        log(f"scan complete, sleeping {args.scan_interval}s")
        time.sleep(args.scan_interval)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-queue", action="store_true", help="Drain queued audio-complete events and exit")
    parser.add_argument("--idle-exit-delay", type=float, default=10.0, help="Seconds to wait for late events before exit")
    parser.add_argument("--scan-loop", action="store_true", help="Legacy periodic index scan mode")
    parser.add_argument("--scan-interval", type=float, default=60.0, help="Seconds between index scans in --scan-loop mode")
    parser.add_argument("--once", action="store_true", help="Process one scan round and exit")
    parser.add_argument("--slug", action="append", default=[], help="Process exact slug; repeatable")
    parser.add_argument("--only", action="append", default=[], help="Only process slugs containing this substring")
    args = parser.parse_args()

    if args.scan_loop and args.event_queue:
        parser.error("--scan-loop and --event-queue are mutually exclusive")

    with exclusive_worker_lock() as have_lock:
        if not have_lock:
            log("another video worker is active; event will be handled by that process")
            return 0
        if args.scan_loop:
            return run_scan_loop(args)
        if args.once or args.slug or args.only:
            log(f"one-shot worker starting; release={RELEASE_TAG}")
            process_ready(slugs=args.slug or None, only_filters=args.only)
            return 0
        return run_event_queue(args.idle_exit_delay)


if __name__ == "__main__":
    sys.exit(main())
