"""
Auto Edit — ใส่คลิป → ตัดต่ออัตโนมัติ → ได้คลิปพร้อมซับไทย
- ตัดช่วงเงียบ (เว้นขอบไว้ ไม่กินคำ)
- ถอดเสียงไทยในเครื่อง (ฟรี ไม่ใช้ API)
- ฝังซับไทย + ได้ไฟล์ .srt แยก
- เฝ้าโฟลเดอร์ตลอด ทำทีละคลิปจนหมดคิว
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "ตั้งค่า.json"
FIX_FILE = BASE / "คำแก้.txt"
LOG_FILE = BASE / "บันทึกการทำงาน.txt"
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".m4v", ".avi", ".webm"}

DEFAULT_CONFIG = {
    "โฟลเดอร์คลิปเข้า": str(BASE / "1_ใส่คลิปตรงนี้"),
    "โฟลเดอร์ผลงาน": str(BASE / "2_ผลงานเสร็จ"),
    "โฟลเดอร์ต้นฉบับที่ทำแล้ว": str(BASE / "3_ต้นฉบับที่ทำแล้ว"),
    "โฟลเดอร์มีปัญหา": str(BASE / "4_มีปัญหา"),
    # ตัดเงียบ
    "ความดังที่ถือว่าเงียบ_dB": -35,
    "เงียบนานเกินกี่วินาทีถึงตัด": 0.6,
    "เว้นขอบก่อนหลังคำพูด_วินาที": 0.2,
    # ซับ
    "โมเดลถอดเสียง": "large-v3",
    "คำช่วยถอดเสียง": "โจ้ข่าวฉาว ข่าวนโยบายรัฐ สิทธิ์ประชาชน",
    "ตัวอักษรต่อบรรทัด": 22,
    "ฟอนต์": "Leelawadee UI",
    "ขนาดฟอนต์": 16,
    "ฝังซับในคลิป": True,
    "ตรวจโฟลเดอร์ทุกกี่วินาที": 20,
}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return cfg


def load_fixes():
    fixes = []
    if FIX_FILE.exists():
        for line in FIX_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                wrong, right = line.split("=", 1)
                if wrong.strip():
                    fixes.append((wrong.strip(), right.strip()))
    return fixes


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])
    return p


def duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(p.stdout.strip())


def has_audio(path):
    p = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)])
    return bool(p.stdout.strip())


# ---------- 1) ตัดช่วงเงียบ ----------
def find_keep_ranges(path, cfg):
    total = duration(path)
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(path), "-af",
         f"silencedetect=noise={cfg['ความดังที่ถือว่าเงียบ_dB']}dB:d={cfg['เงียบนานเกินกี่วินาทีถึงตัด']}",
         "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", p.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", p.stderr)]
    if len(ends) < len(starts):
        ends.append(total)
    pad = cfg["เว้นขอบก่อนหลังคำพูด_วินาที"]
    keep, cur = [], 0.0
    for s, e in zip(starts, ends):
        cut_from, cut_to = s + pad, e - pad
        if cut_to - cut_from < 0.1:
            continue
        if cut_from > cur:
            keep.append((cur, cut_from))
        cur = cut_to
    if cur < total:
        keep.append((cur, total))
    keep = [(a, b) for a, b in keep if b - a > 0.05]
    return keep or [(0.0, total)], total


def cut_silence(src, dst, cfg, tmp):
    if not has_audio(src):
        shutil.copy(src, dst)
        return
    keep, total = find_keep_ranges(src, cfg)
    kept = sum(b - a for a, b in keep)
    log(f"   ตัดเงียบ: {total:.1f} วิ → {kept:.1f} วิ ({len(keep)} ช่วง)")
    parts = []
    for i, (a, b) in enumerate(keep):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}];")
    parts.append("".join(f"[v{i}][a{i}]" for i in range(len(keep))) + f"concat=n={len(keep)}:v=1:a=1[v][a]")
    script = Path(tmp) / "cut.txt"
    script.write_text("\n".join(parts), encoding="utf-8")
    run(["ffmpeg", "-y", "-hide_banner", "-i", str(src), "-filter_complex_script", str(script),
         "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-c:a", "aac", "-b:a", "192k", str(dst)])


# ---------- 2) ถอดเสียงเป็นซับไทย ----------
_model = None


def get_model(cfg):
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log(f"   โหลดโมเดลถอดเสียง {cfg['โมเดลถอดเสียง']} (ครั้งแรกจะดาวน์โหลด รอสักครู่)")
        try:
            _model = WhisperModel(cfg["โมเดลถอดเสียง"], device="cuda", compute_type="float16")
            log("   ใช้การ์ดจอเร่งความเร็ว")
        except Exception:
            _model = WhisperModel(cfg["โมเดลถอดเสียง"], device="cpu", compute_type="int8")
    return _model


_dict = None


def thai_words(text, keep_words=()):
    global _dict
    from pythainlp.tokenize import word_tokenize
    from pythainlp.corpus.common import thai_words as base_words
    from pythainlp.util import dict_trie
    if _dict is None:
        _dict = dict_trie(set(base_words()) | {w for w in keep_words if w})
    return word_tokenize(text, custom_dict=_dict, engine="newmm", keep_whitespace=True)


def split_line(text, start, end, max_chars, keep_words=()):
    """แบ่งประโยคยาวเป็นท่อนสั้น ไม่ตัดกลางคำ แบ่งเวลาตามจำนวนตัวอักษร"""
    if len(text) <= max_chars:
        return [(start, end, text)]
    chunks, cur = [], ""
    for w in thai_words(text, keep_words):
        if len(cur) + len(w) > max_chars and cur.strip():
            chunks.append(cur.strip())
            cur = w
        else:
            cur += w
    if cur.strip():
        if chunks and len(cur.strip()) < 6:
            chunks[-1] += cur.rstrip()  # ไม่ให้คำสั้นๆ ค้างบรรทัดเดียว
        else:
            chunks.append(cur.strip())
    total_chars = sum(len(c) for c in chunks) or 1
    out, t = [], start
    for c in chunks:
        d = (end - start) * len(c) / total_chars
        out.append((t, t + d, c))
        t += d
    return out


def transcribe(video, cfg, fixes):
    model = get_model(cfg)
    segments, _ = model.transcribe(
        str(video), language="th", beam_size=5, vad_filter=True,
        initial_prompt=cfg["คำช่วยถอดเสียง"] or None, condition_on_previous_text=False)
    keep_words = cfg["คำช่วยถอดเสียง"].split() + [r for _, r in fixes]
    lines = []
    for seg in segments:
        text = seg.text.strip()
        for wrong, right in fixes:
            text = text.replace(wrong, right)
        if text:
            lines.extend(split_line(text, seg.start, seg.end, cfg["ตัวอักษรต่อบรรทัด"], keep_words))
    return lines


def ts(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(lines, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, t) in enumerate(lines, 1):
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{t}\n\n")


# ---------- 3) ฝังซับ ----------
def burn_subs(video, srt, dst, cfg, tmp):
    # คัดลอก srt ไปไว้ชื่อง่ายๆ เพื่อเลี่ยงปัญหา path ภาษาไทย/ไดรฟ์ใน ffmpeg
    simple = Path(tmp) / "sub.srt"
    shutil.copy(srt, simple)
    style = (f"FontName={cfg['ฟอนต์']},FontSize={cfg['ขนาดฟอนต์']},PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=40")
    p = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-i", str(video), "-vf",
                        f"subtitles=sub.srt:force_style='{style}'", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "18", "-c:a", "copy", str(dst)],
                       cwd=tmp, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError(p.stderr[-2000:])


# ---------- งานหลัก ----------
def process(src, cfg, fixes):
    out_dir = Path(cfg["โฟลเดอร์ผลงาน"])
    name = src.stem
    with tempfile.TemporaryDirectory() as tmp:
        cut = Path(tmp) / "cut.mp4"
        cut_silence(src, cut, cfg, tmp)
        log("   กำลังถอดเสียงทำซับ...")
        lines = transcribe(cut, cfg, fixes)
        srt = out_dir / f"{name}.srt"
        write_srt(lines, srt)
        final = Path(tmp) / "final.mp4"
        if cfg["ฝังซับในคลิป"] and lines:
            log("   กำลังฝังซับ...")
            burn_subs(cut, srt, final, cfg, tmp)
        else:
            shutil.copy(cut, final)
        shutil.move(str(final), out_dir / f"{name}_ตัดแล้ว.mp4")


def is_ready(path):
    """ไฟล์ต้องขนาดคงที่ (copy/ซิงก์ Drive เสร็จแล้ว) ถึงจะเริ่มทำ"""
    try:
        s1 = path.stat().st_size
        time.sleep(3)
        return s1 > 0 and s1 == path.stat().st_size
    except OSError:
        return False


def safe_move(src, folder):
    folder.mkdir(parents=True, exist_ok=True)
    dst = folder / src.name
    if dst.exists():
        dst = folder / f"{src.stem}_{int(time.time())}{src.suffix}"
    shutil.move(str(src), dst)


def main():
    cfg = load_config()
    folders = {k: Path(cfg[k]) for k in
               ["โฟลเดอร์คลิปเข้า", "โฟลเดอร์ผลงาน", "โฟลเดอร์ต้นฉบับที่ทำแล้ว", "โฟลเดอร์มีปัญหา"]}
    for f in folders.values():
        f.mkdir(parents=True, exist_ok=True)
    log(f"เริ่มทำงาน — รอคลิปที่: {folders['โฟลเดอร์คลิปเข้า']}")
    once = "--once" in sys.argv
    while True:
        cfg = load_config()
        fixes = load_fixes()
        clips = sorted(p for p in folders["โฟลเดอร์คลิปเข้า"].iterdir()
                       if p.is_file() and p.suffix.lower() in VIDEO_EXT)
        for i, clip in enumerate(clips, 1):
            if not is_ready(clip):
                continue
            log(f"[{i}/{len(clips)}] {clip.name}")
            t0 = time.time()
            try:
                process(clip, cfg, fixes)
                safe_move(clip, folders["โฟลเดอร์ต้นฉบับที่ทำแล้ว"])
                log(f"   ✅ เสร็จ ({time.time() - t0:.0f} วิ)")
            except Exception:
                log("   ❌ มีปัญหา ย้ายไปโฟลเดอร์มีปัญหา\n" + traceback.format_exc()[-1500:])
                try:
                    safe_move(clip, folders["โฟลเดอร์มีปัญหา"])
                except Exception:
                    pass
        if once:
            break
        time.sleep(cfg["ตรวจโฟลเดอร์ทุกกี่วินาที"])


if __name__ == "__main__":
    main()
