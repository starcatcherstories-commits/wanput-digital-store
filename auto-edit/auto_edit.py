import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = ROOT / "settings.json"
DEFAULT_SETTINGS = {
    "input_folder": "",
    "output_folder": "",
    "threshold": -38,
    "min_silence": 0.6,
    "keep_pause": 0.3,
    "max_caption_chars": 28,
    "remove_retakes": True,
    "remove_fillers": True,
    "fillers": ["เอ่อ", "อ่า", "อืม", "เออ", "เอิ่ม", "อ้า", "อึม", "เอ่"],
}
if not SETTINGS_FILE.exists():
    SETTINGS_FILE.write_text(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
SETTINGS = {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))}
# Empty = use the input/output folders next to this program. Can point to Google Drive (e.g. G:/My Drive/...).
INPUT = Path(SETTINGS["input_folder"]) if SETTINGS["input_folder"] else ROOT / "input"
OUTPUT = Path(SETTINGS["output_folder"]) if SETTINGS["output_folder"] else ROOT / "output"
FIXES_FILE = ROOT / "word-fixes.txt"
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


def cut_and_join(source, ranges, stage, video, output_name, prefix="part"):
    segments = []
    for index, (start, end) in enumerate(ranges):
        part = f"{prefix}-{index:05}.{'mp4' if video else 'm4a'}"
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
    playlist = stage / f"{prefix}-segments.txt"
    playlist.write_text("".join(f"file '{part}'\n" for part in segments), encoding="ascii")
    run(FFMPEG, "-v", "error", "-f", "concat", "-i", playlist.name,
        "-c", "copy", "-movflags", "+faststart", output_name, cwd=stage)
    for part in segments:
        (stage / part).unlink(missing_ok=True)


def move_into(source, destination):
    """Move a finished file into output, also when output is on another drive (e.g. Google Drive)."""
    source, destination = Path(source), Path(destination)
    try:
        os.replace(source, destination)
    except OSError:
        partial = destination.with_name(destination.name + ".partial")
        shutil.copyfile(source, partial)
        os.replace(partial, destination)
        source.unlink(missing_ok=True)


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
            "remove_retakes": SETTINGS["remove_retakes"], "remove_fillers": SETTINGS["remove_fillers"],
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


def load_fixes():
    fixes = []
    if FIXES_FILE.exists():
        for line in FIXES_FILE.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                wrong, right = (part.strip() for part in line.split("=", 1))
                if wrong:
                    fixes.append((wrong, right))
    return fixes


def thai_pieces(text):
    """Split text into pieces that are safe to break between (never inside a Thai word)."""
    try:
        from pythainlp.tokenize import word_tokenize
        return word_tokenize(text, engine="newmm", keep_whitespace=True)
    except Exception:
        return re.split(r"(\s+)", text)


def split_cues(cues, max_chars):
    result = []
    for cue in cues:
        text = cue["text"]
        if len(text) <= max_chars:
            result.append(cue)
            continue
        lines, current = [], ""
        for piece in thai_pieces(text):
            if len(current) + len(piece) > max_chars and current.strip():
                lines.append(current.strip())
                current = piece
            else:
                current += piece
        if current.strip():
            if lines and len(current.strip()) < 6:
                lines[-1] += current.rstrip()
            else:
                lines.append(current.strip())
        total = sum(len(line) for line in lines) or 1
        start, span = cue["start"], cue["end"] - cue["start"]
        for line in lines:
            end = start + span * len(line) / total
            result.append({"start": round(start), "end": round(end), "text": line})
            start = end
        result[-1]["end"] = cue["end"]
    return result


def normalize(text):
    return re.sub(r"[\s.,!?…\"'“”‘’\-–—:;()\[\]]+", "", text)


def strip_fillers(text, fillers):
    if not fillers:
        return text
    pattern = "|".join(re.escape(word) + "+" for word in sorted(fillers, key=len, reverse=True))
    text = re.sub(rf"(?:^|(?<=\s))(?:{pattern})(?=\s|$)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def find_content_cuts(cues):
    """Decide which spoken lines to drop: repeated takes, false starts and lines that are only fillers."""
    from difflib import SequenceMatcher
    fillers = SETTINGS["fillers"] if SETTINGS["remove_fillers"] else []
    filler_only = re.compile("(?:" + "|".join(re.escape(w) + "+" for w in fillers) + ")+") if fillers else None
    drops = {}
    for i, cue in enumerate(cues):
        a = normalize(cue["text"])
        if filler_only and (not a or filler_only.fullmatch(a)):
            drops[i] = "filler"
            continue
        if not SETTINGS["remove_retakes"] or len(a) < 4:
            continue
        for later in cues[i + 1:i + 3]:
            if later["start"] - cue["end"] > 8000:
                break
            b = normalize(strip_fillers(later["text"], fillers))
            if len(b) < 4:
                continue
            if b.startswith(a) or SequenceMatcher(None, a, b[:len(a)]).ratio() >= 0.8:
                drops[i] = "retake"
                break
    return drops


def apply_content_cuts(stage, media, cues, duration_ms):
    drops = find_content_cuts(cues)
    if not drops:
        return cues, [], duration_ms
    removed_spans = []
    for i, reason in sorted(drops.items()):
        start = cues[i]["start"]
        end = cues[i + 1]["start"] - 120 if i + 1 < len(cues) else duration_ms
        if end - start > 50:
            removed_spans.append((start, end, cues[i]["text"], reason))
    merged = []
    for start, end, *_ in removed_spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    keep, cursor = [], 0
    for start, end in merged:
        if start - cursor > 30:
            keep.append((cursor / 1000, start / 1000))
        cursor = end
    if duration_ms - cursor > 30:
        keep.append((cursor / 1000, duration_ms / 1000))
    if not keep:
        return cues, [], duration_ms
    video = media.suffix.lower() in VIDEO_TYPES
    cut_and_join(media, keep, stage, video, f"clean{media.suffix}", prefix="clean")
    os.replace(stage / f"clean{media.suffix}", media)

    def shift(t):
        return t - sum(min(end, t) - start for start, end in merged if start < t)

    kept = []
    for i, cue in enumerate(cues):
        if i in drops:
            continue
        start, end = shift(cue["start"]), shift(cue["end"])
        if end - start > 50:
            kept.append({**cue, "start": round(start), "end": round(end)})
    new_duration = sum(b - a for a, b in keep) * 1000
    removed = [{"text": text, "reason": reason, "seconds": round((end - start) / 1000, 2)}
               for start, end, text, reason in removed_spans]
    return kept, removed, new_duration


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
    fixes = load_fixes()
    for segment in data["transcription"]:
        for wrong, right in fixes:
            segment["text"] = segment["text"].replace(wrong, right)
    cues, warnings = validate_transcript(data, duration_ms)
    cues, removed, duration_ms = apply_content_cuts(stage, media, cues, duration_ms)
    if removed:
        print(f"  Removed {len(removed)} retakes/fillers "
              f"({sum(r['seconds'] for r in removed):.1f}s)", flush=True)
    if SETTINGS["remove_fillers"]:
        cues = [{**c, "text": strip_fillers(c["text"], SETTINGS["fillers"])} for c in cues]
        cues = [c for c in cues if c["text"]]
    cues = split_cues(cues, int(SETTINGS["max_caption_chars"]))
    subtitles = stage / "captions.srt"
    write_srt(subtitles, cues)
    report = {"model": MODEL.name, "language": language,
              "duration_ms": round(duration_ms), "caption_count": len(cues),
              "warnings": warnings, "content_removed": removed}
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
        move_into(stage / "captions.srt", final_srt)
        move_into(completed, media)
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
        joined = stage / f"joined{media.suffix}"
        cut_and_join(source, ranges, stage, video, joined.name)

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
            review.mkdir(parents=True, exist_ok=True)
            report["source_signature"] = list(signature)
            report["status"] = "draft"
            report["draft_sha256"] = file_digest(joined)
            report["media_destination"] = str(media.relative_to(OUTPUT))
            report["subtitle_destination"] = str(srt.relative_to(OUTPUT))
            move_into(subtitles, review / srt.name)
            move_into(joined, review / media.name)
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
        move_into(subtitles, srt)
        move_into(final, media)
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
                if not args.once and time.monotonic() - first_seen < 15:
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
                        if (completed.get("source_sha256") == source_hash
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


def find_google_drive():
    for letter in "GHIJKLMNOPQRSTUVWXYZDEF":
        for name in ("My Drive", "ไดรฟ์ของฉัน"):
            base = Path(f"{letter}:/{name}")
            try:
                if base.is_dir():
                    return base
            except OSError:
                pass
    return None


def setup():
    """One-time setup: use Google Drive automatically when it is installed."""
    global INPUT, OUTPUT
    if not SETTINGS["input_folder"]:
        drive = find_google_drive()
        if drive:
            SETTINGS["input_folder"] = str(drive / "AutoEdit" / "ใส่คลิป")
            SETTINGS["output_folder"] = str(drive / "AutoEdit" / "ผลงาน")
            SETTINGS_FILE.write_text(json.dumps(SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
            INPUT, OUTPUT = Path(SETTINGS["input_folder"]), Path(SETTINGS["output_folder"])
    INPUT.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    print(f"ใส่คลิปที่:   {INPUT}\nรับผลงานที่: {OUTPUT}", flush=True)
    if hasattr(os, "startfile"):
        os.startfile(INPUT)
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Cut silence and caption media from input/.")
    parser.add_argument("--once", action="store_true", help="Process existing files and exit.")
    parser.add_argument("--stop", action="store_true", help="Stop the watcher after the current file.")
    parser.add_argument("--setup", action="store_true", help="One-time setup (finds Google Drive).")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--draft", dest="draft", action="store_true", help="Write video and SRT to output/รอตรวจ for checking (optional).")
    mode.add_argument("--burn-subtitles", dest="draft", action="store_false", help="Burn subtitles automatically (default); warnings are recorded in the quality report.")
    parser.set_defaults(draft=False)
    parser.add_argument("--replace-existing", action="store_true", help="Regenerate existing output or a draft with a different source identity.")
    parser.add_argument("--approve", type=Path, metavar="SRT", help="Burn an edited/reviewed SRT from output/รอตรวจ without retranscribing.")
    parser.add_argument("--cover-old-subtitles", action="store_true", help="With --approve, cover the bottom 20%% of a video before burning the reviewed SRT.")
    parser.add_argument("--language", default="th", help="Spoken language (default: th). Use auto for mixed batches.")
    parser.add_argument("--threshold", type=float, default=SETTINGS["threshold"], help="Silence threshold in dB.")
    parser.add_argument("--min-silence", type=float, default=SETTINGS["min_silence"])
    parser.add_argument("--keep-pause", type=float, default=SETTINGS["keep_pause"])
    args = parser.parse_args()
    if args.cover_old_subtitles and not args.approve:
        parser.error("--cover-old-subtitles requires --approve.")
    if args.setup:
        return setup()
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
    INPUT.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
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
