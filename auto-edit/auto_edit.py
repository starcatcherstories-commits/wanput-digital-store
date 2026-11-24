import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent
INPUT = ROOT / "input"
OUTPUT = ROOT / "output"
RUNTIME = ROOT / ".runtime"
FFMPEG = RUNTIME / "ffmpeg.exe"
FFPROBE = RUNTIME / "ffprobe.exe"
WHISPER = RUNTIME / "whisper" / "Release" / "whisper-cli.exe"
MODEL = ROOT / ".models" / "ggml-large-v3.bin"
VAD_MODEL = ROOT / ".models" / "ggml-silero-v5.1.2.bin"
STOP_REQUEST = RUNTIME / "stop.request"
GENRE_MAP_FILE = ROOT / "genre-map.json"
GENRE_MAP = json.loads(GENRE_MAP_FILE.read_text(encoding="utf-8")) if GENRE_MAP_FILE.exists() else {}
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi", ".mts"}
AUDIO_TYPES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
SILENCE_EVENT = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)")
PIPELINE_VERSION = 3


def run(program, *arguments, cwd=None):
    result = subprocess.run(
        [str(program), *map(str, arguments)],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        raise RuntimeError(f"{program.name} exited with {result.returncode}\n"
                           f"{result.stderr.strip()[-3000:]}")
    return result


def keep_ranges(log, duration, pause):
    cuts = []
    start = None
    # Keep a small breath at both sides of an internal cut.
    for kind, value in SILENCE_EVENT.findall(log):
        position = min(duration, max(0.0, float(value)))
        if kind == "start":
            start = position
        elif start is not None:
            left = start + (pause / 2 if start > 0.03 else 0)
            right = position - (pause / 2 if position < duration - 0.03 else 0)
            if right - left > 0.05:
                cuts.append((left, right))
            start = None
    if start is not None and duration > start:
        cuts.append((start + (pause / 2 if start > 0.03 else 0), duration))

    cursor = 0.0
    ranges = []
    for left, right in cuts:
        if left - cursor > 0.03:
            ranges.append((cursor, left))
        cursor = max(cursor, right)
    if duration - cursor > 0.03:
        ranges.append((cursor, duration))
    return ranges


def destinations(source):
    name = source.stem if source.suffix.lower() == ".mp4" else source.name
    audio = source.suffix.lower() in AUDIO_TYPES
    extension = "m4a" if audio else "mp4"
    destination = OUTPUT / "พร้อมใช้" / name
    return destination / f"{name}.edited.{extension}", destination / "ซับ" / f"{name}.edited.srt"


def srt_time(milliseconds):
    seconds, milliseconds = divmod(round(milliseconds), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def processing_settings(args):
    return {"pipeline_version": PIPELINE_VERSION, "model": MODEL.name,
            "language": args.language, "threshold": args.threshold,
            "min_silence": args.min_silence, "keep_pause": args.keep_pause}


def write_srt(path, cues):
    path.write_text("\n".join(
        f"{index}\n{srt_time(cue['start'])} --> {srt_time(cue['end'])}\n{cue['text']}\n"
        for index, cue in enumerate(cues, 1)), encoding="utf-8")


def validate_transcript(data, duration_ms):
    cues = []
    warnings = []
    previous_end = 0
    for segment in data["transcription"]:
        text = segment["text"].strip()
        if not text or re.fullmatch(r"\[[^\]]*\]|\([^)]*\)", text):
            continue
        if "\ufffd" in text:
            raise ValueError("Caption text contains damaged Unicode. Output was not replaced.")
        start = segment["offsets"]["from"]
        end = segment["offsets"]["to"]
        index = len(cues) + 1
        if start < 0 or end <= start or end > duration_ms + 1000:
            raise ValueError(f"Caption {index} has timestamps outside the edited audio.")
        if start < previous_end - 100:
            raise ValueError(f"Caption {index} overlaps the previous caption.")
        start = max(previous_end, min(start, duration_ms))
        end = min(end, duration_ms)
        if end <= start:
            raise ValueError(f"Caption {index} has no display time.")
        probabilities = [token["p"] for token in segment.get("tokens", [])
                         if token["text"].strip() and not token["text"].startswith(("<|", "[_"))]
        confidence = sum(probabilities) / len(probabilities) if probabilities else None
        reasons = []
        if confidence is not None and confidence < 0.65:
            reasons.append("low_recognition_confidence")
        if end - start > 10000:
            reasons.append("long_caption_check_timing")
        if len(text) / ((end - start) / 1000) > 35:
            reasons.append("caption_too_fast")
        if reasons:
            warnings.append({"cue": index, "text": text, "confidence": confidence,
                             "reasons": reasons})
        cues.append({"start": start, "end": end, "text": text})
        previous_end = end
    if not cues:
        raise ValueError("No speech captions were recognized. Output was not replaced.")
    return cues, warnings


def transcribe(stage, media, language):
    run(FFMPEG, "-v", "error", "-i", media.name,
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "speech.wav", cwd=stage)
    # This Windows whisper-cli build cannot open non-ASCII model arguments.
    # Model and staging directories share a drive, so relative paths stay ASCII.
    run(WHISPER, "-m", os.path.relpath(MODEL, stage),
        "-vm", os.path.relpath(VAD_MODEL, stage), "--vad", "-f", "speech.wav",
        "-l", language, "-ng", "-ojf", "-of", "captions",
        "-ml", "60", "-sow", "-mc", "0", "-t", min(8, os.cpu_count() or 4), cwd=stage)
    duration_ms = float(json.loads(run(FFPROBE, "-v", "error", "-show_format",
                                      "-of", "json", media).stdout)["format"]["duration"]) * 1000
    # whisper.cpp writes partial UTF-8 bytes for individual Thai tokens in full JSON.
    # Segment text remains complete. Replacement is limited to decoding token fragments.
    data = json.loads((stage / "captions.json").read_text(encoding="utf-8", errors="replace"))
    cues, warnings = validate_transcript(data, duration_ms)
    subtitles = stage / "captions.srt"
    write_srt(subtitles, cues)
    report = {"model": MODEL.name, "language": language,
              "duration_ms": round(duration_ms), "caption_count": len(cues),
              "warnings": warnings}
    return subtitles, report


def archive_review(draft, subtitles, report_path):
    archive = RUNTIME / "ซับที่ตรวจแล้ว"
    archive.mkdir(exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="review-", dir=archive))
    moved = []
    try:
        for path in (draft, subtitles, report_path):
            path.rename(destination / path.name)
            moved.append(path)
    except OSError:
        for path in reversed(moved):
            (destination / path.name).rename(path)
        destination.rmdir()
        raise


def render_reviewed(subtitles, cover_old_subtitles=False):
    subtitles = subtitles.resolve()
    review = (OUTPUT / "รอตรวจ").resolve()
    if subtitles.parent != review or subtitles.suffix.lower() != ".srt":
        raise ValueError("Select an SRT file from output/รอตรวจ.")
    report_path = subtitles.with_suffix(".review.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    text = subtitles.read_text(encoding="utf-8-sig").strip()
    segments = []
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].isdigit():
            raise ValueError("Invalid SRT cue.")
        match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d),(\d{3}) --> (\d+):([0-5]\d):([0-5]\d),(\d{3})", lines[1])
        if not match:
            raise ValueError("Invalid SRT timestamps.")
        numbers = list(map(int, match.groups()))
        def milliseconds(values):
            hours, minutes, seconds, fraction = values
            return ((hours * 60 + minutes) * 60 + seconds) * 1000 + fraction
        segments.append({"text": "\n".join(lines[2:]), "offsets": {
            "from": milliseconds(numbers[:4]), "to": milliseconds(numbers[4:])}})
    media = (OUTPUT / report["media_destination"]).resolve()
    final_srt = (OUTPUT / report["subtitle_destination"]).resolve()
    if not media.is_relative_to(OUTPUT.resolve()) or not final_srt.is_relative_to(OUTPUT.resolve()):
        raise ValueError("Invalid review destination.")
    draft = review / media.name
    if cover_old_subtitles and media.suffix.lower() not in VIDEO_TYPES:
        raise ValueError("Covering old subtitles requires a video draft.")
    if report.get("draft_sha256") != file_digest(draft):
        raise ValueError("The draft video differs from the caption review. Generate a new matching draft.")
    duration_ms = float(json.loads(run(FFPROBE, "-v", "error", "-show_format",
                                      "-of", "json", draft).stdout)["format"]["duration"]) * 1000
    cues, _ = validate_transcript({"transcription": segments}, duration_ms)
    with tempfile.TemporaryDirectory(prefix=".review-", dir=RUNTIME) as directory:
        stage = Path(directory)
        write_srt(stage / "captions.srt", cues)
        completed = stage / media.name
        if media.suffix.lower() in VIDEO_TYPES:
            filters = "subtitles=captions.srt:force_style='FontName=Leelawadee UI,FontSize=13,Outline=2,MarginV=24'"
            if cover_old_subtitles:
                filters = "drawbox=x=0:y=ih*0.80:w=iw:h=ih*0.20:color=black:t=fill," + filters
            run(FFMPEG, "-v", "error", "-i", draft,
                "-vf", filters,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                "-c:a", "copy", "-movflags", "+faststart", completed, cwd=stage)
        else:
            import shutil
            shutil.copy2(draft, completed)
        media.parent.mkdir(parents=True, exist_ok=True)
        final_srt.parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage / "captions.srt", final_srt)
        os.replace(completed, media)
        report["status"] = "reviewed"
        report["covered_old_subtitles"] = cover_old_subtitles
        report["caption_count"] = len(cues)
        report["final_srt_sha256"] = file_digest(final_srt)
        report["final_media_sha256"] = file_digest(media)
        final_srt.with_suffix(".quality.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_review(draft, subtitles, report_path)
    print(f"Saved reviewed captions: {media}", flush=True)


def process(source, signature, args):
    media, srt = destinations(source)
    source_hash = file_digest(source)
    if fingerprint(source) != signature:
        raise ValueError("The source is still changing. Wait for the copy to finish.")
    details = json.loads(run(
        FFPROBE, "-v", "error", "-show_format", "-show_streams",
        "-of", "json", source,
    ).stdout)
    duration = float(details["format"]["duration"])
    streams = details["streams"]
    if not any(stream["codec_type"] == "audio" for stream in streams):
        raise ValueError("The source has no audio track.")
    video = source.suffix.lower() in VIDEO_TYPES
    detected = run(
        FFMPEG, "-hide_banner", "-i", source, "-map", "0:a:0",
        "-af", f"silencedetect=n={args.threshold}dB:d={args.min_silence}",
        "-f", "null", "-",
    ).stderr
    ranges = keep_ranges(detected, duration, args.keep_pause)
    if not ranges:
        raise ValueError("The source appears to be entirely silent.")
    print(f"  Keeping {sum(end - start for start, end in ranges):.1f}s of "
          f"{duration:.1f}s ({len(ranges)} segments)", flush=True)

    with tempfile.TemporaryDirectory(prefix=".edit-", dir=RUNTIME) as directory:
        stage = Path(directory)
        segments = []
        for index, (start, end) in enumerate(ranges):
            part = f"part-{index:05}.{'mp4' if video else 'm4a'}"
            video_options = (
                ["-map", "0:V:0", "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                 "-pix_fmt", "yuv420p"]
                if video else ["-vn"]
            )
            run(
                FFMPEG, "-v", "error", "-ss", f"{start:.6f}", "-i", source,
                "-t", f"{end - start:.6f}", *video_options, "-map", "0:a:0",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000", part, cwd=stage,
            )
            segments.append(part)

        playlist = stage / "segments.txt"
        playlist.write_text("".join(f"file '{part}'\n" for part in segments),
                            encoding="ascii")
        joined = stage / f"joined{media.suffix}"
        run(FFMPEG, "-v", "error", "-f", "concat", "-i", playlist.name,
            "-c", "copy", "-movflags", "+faststart", joined.name, cwd=stage)

        print("  Transcribing...", flush=True)
        subtitles, report = transcribe(stage, joined, args.language)
        report["source_sha256"] = source_hash
        report["source_path"] = str(source.resolve())
        report["settings"] = processing_settings(args)
        report["original_duration_seconds"] = duration
        report["kept_ranges_seconds"] = ranges
        report["silence_removed_seconds"] = duration - sum(b-a for a,b in ranges)
        caption_count = report["caption_count"]
        if fingerprint(source) != signature or file_digest(source) != source_hash:
            raise ValueError("The source changed during transcription. Output was not replaced.")
        if args.draft:
            review = OUTPUT / "รอตรวจ"
            review.mkdir(exist_ok=True)
            report["source_signature"] = list(signature)
            report["status"] = "draft"
            report["draft_sha256"] = file_digest(joined)
            report["media_destination"] = str(media.relative_to(OUTPUT))
            report["subtitle_destination"] = str(srt.relative_to(OUTPUT))
            os.replace(subtitles, review / srt.name)
            os.replace(joined, review / media.name)
            # The report is the completion marker; write it after both draft files.
            (review / f"{media.stem}.review.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  Caption draft: {review / srt.name}. Captions were not burned.", flush=True)
            return
        final = joined
        if video and caption_count:
            final = stage / "captioned.mp4"
            run(FFMPEG, "-v", "error", "-i", joined.name,
                "-vf", "subtitles=captions.srt:force_style='FontName=Leelawadee UI,FontSize=13,Outline=2,MarginV=24'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
                "-c:a", "copy", "-movflags", "+faststart", final.name, cwd=stage)
        if fingerprint(source) != signature:
            raise ValueError("The source changed during processing. It will be picked up again.")
        media.parent.mkdir(parents=True, exist_ok=True)
        srt.parent.mkdir(parents=True, exist_ok=True)
        os.replace(subtitles, srt)
        os.replace(final, media)
        report["status"] = "automatic"
        report["final_srt_sha256"] = file_digest(srt)
        report["final_media_sha256"] = file_digest(media)
        srt.with_suffix(".quality.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Saved {media.name} + {srt.name} ({caption_count} captions)", flush=True)


def fingerprint(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def watch(args):
    observed = {}
    attempted = {}
    failed = False
    print(f"Input:  {INPUT}\nOutput: {OUTPUT}", flush=True)
    if not args.once:
        print("Watching for files. Press Ctrl+C to stop.", flush=True)
    while True:
        files = sorted(INPUT.iterdir())
        present = set(files)
        observed = {k: v for k, v in observed.items() if k in present}
        attempted = {k: v for k, v in attempted.items() if k in present}
        for source in files:
            if not args.once and STOP_REQUEST.exists():
                break
            if not source.is_file() or source.suffix.lower() not in VIDEO_TYPES | AUDIO_TYPES:
                continue
            try:
                signature = fingerprint(source)
                prior_signature, first_seen = observed.get(source, (None, 0.0))
                if prior_signature != signature:
                    first_seen = time.monotonic()
                    observed[source] = signature, first_seen
                if not args.once and time.monotonic() - first_seen < 5:
                    continue
                if not signature[0] or attempted.get(source) == signature:
                    continue
                attempted[source] = signature
                media, srt = destinations(source)
                source_hash = file_digest(source)
                settings = processing_settings(args)
                review_report = OUTPUT / "รอตรวจ" / f"{media.stem}.review.json"
                if args.draft and review_report.exists():
                    review = json.loads(review_report.read_text(encoding="utf-8"))
                    draft_media = review_report.parent / media.name
                    draft_srt = review_report.parent / srt.name
                    if (review.get("status") == "draft" and review.get("source_sha256") == source_hash and review.get("settings") == settings
                            and draft_media.is_file() and draft_srt.is_file()
                            and review.get("draft_sha256") == file_digest(draft_media)):
                        continue
                    if review.get("status") != "reviewed" and not args.replace_existing:
                        raise FileExistsError("A different or incomplete draft exists. Use --replace-existing to replace it.")
                quality = srt.with_suffix(".quality.json")
                if media.exists() or srt.exists():
                    if media.is_file() and srt.is_file() and quality.is_file():
                        completed = json.loads(quality.read_text(encoding="utf-8"))
                        if (completed.get("source_sha256") == source_hash and completed.get("settings") == settings
                                and completed.get("final_media_sha256") == file_digest(media)
                                and completed.get("final_srt_sha256") == file_digest(srt)):
                            continue
                    if not args.replace_existing:
                        raise FileExistsError("Existing output has no matching source identity. Use --replace-existing to regenerate it.")
                print(f"Editing {source.name}", flush=True)
                process(source, signature, args)
            except Exception as error:
                failed = True
                print(f"Could not edit {source.name}: {error}", file=sys.stderr, flush=True)
        if args.once:
            return int(failed)
        if STOP_REQUEST.exists():
            STOP_REQUEST.unlink(missing_ok=True)
            print("Stopped.", flush=True)
            return 0
        time.sleep(2)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Cut silence and caption media from input/.")
    parser.add_argument("--once", action="store_true", help="Process existing files and exit.")
    parser.add_argument("--stop", action="store_true", help="Stop the watcher after the current file.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--draft", dest="draft", action="store_true", help="Write video and SRT to output/รอตรวจ for checking (optional).")
    mode.add_argument("--burn-subtitles", dest="draft", action="store_false", help="Burn subtitles automatically (default); warnings are recorded in the quality report.")
    parser.set_defaults(draft=False)
    parser.add_argument("--replace-existing", action="store_true", help="Regenerate existing output or a draft with a different source identity.")
    parser.add_argument("--approve", type=Path, metavar="SRT", help="Burn an edited/reviewed SRT from output/รอตรวจ without retranscribing.")
    parser.add_argument("--cover-old-subtitles", action="store_true", help="With --approve, cover the bottom 20%% of a video before burning the reviewed SRT.")
    parser.add_argument("--language", default="th", help="Spoken language (default: th). Use auto for mixed batches.")
    parser.add_argument("--threshold", type=float, default=-38, help="Silence threshold in dB.")
    parser.add_argument("--min-silence", type=float, default=0.40)
    parser.add_argument("--keep-pause", type=float, default=0.16)
    args = parser.parse_args()
    if args.cover_old_subtitles and not args.approve:
        parser.error("--cover-old-subtitles requires --approve.")
    if args.stop:
        STOP_REQUEST.touch()
        print("Stop requested. The watcher will finish its current file.")
        return 0
    if not args.min_silence > args.keep_pause >= 0:
        parser.error("Require --min-silence > --keep-pause >= 0.")
    if not FFMPEG.is_file() or not FFPROBE.is_file():
        parser.error("FFmpeg tools are missing from .runtime.")
    if not WHISPER.is_file() or not MODEL.is_file() or not VAD_MODEL.is_file():
        parser.error("Whisper, large-v3, or the Silero VAD model is missing.")
    INPUT.mkdir(exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    import msvcrt

    with (RUNTIME / "watch.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            parser.error("Another editor instance is already open.")
        try:
            if args.approve:
                render_reviewed(args.approve, args.cover_old_subtitles)
                return 0
            if not args.once:
                STOP_REQUEST.unlink(missing_ok=True)
            return watch(args)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
