#!/usr/bin/env python3
"""
Reels Edit (ffmpeg + Python) — SIRF Reels ke liye alag file
=============================================================
Ye movie_automation.py se ALAG file hai. Isme movie wala koi kaam
nahi hai — koi speed, koi mirror, koi fade, koi overlay, koi hourly
effect nahi. Sirf ye karta hai:

1. PARTS - Movie ko 2-2 minute ke parts me kaat do (fast, -c copy).
2. VERTICAL REELS - Har part ka 9:16 vertical Reel banao: poora
   frame beech me, upar-neeche usi video ka BLUR (koi solid color
   nahi, koi kaale bars nahi). Video ke apne kaale bars (letterbox)
   pehle auto-cut ho jate hain (crop), phir blur lagta hai — to
   chalti video ke upar-neeche kaala hissa bhi nahi aata aur blur
   me bhi kaala nahi aata. Length wahi rehti hai, sirf shape
   vertical hota hai (1080x1920).

Usage (GitHub workflows me aise chalta hai):
    python reels_edit.py --split-only movie.mp4 --parts 120 --vertical-parts --reels-only
    python reels_edit.py --split-only movie_processed.mp4 --parts 120 --vertical-parts --reels-only
    python reels_edit.py --vertical-only movie_processed.mp4

    movie.mp4 : YouTube se download ki hui movie, ya pehle se bani
                hui processed movie (movie_processed.mp4)

Requirements: ffmpeg + ffprobe PATH me hone chahiye (aur Python 3).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

def run(cmd, check=True):
    """Ek command chalao; fail ho to saaf error dikhao."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("FAILED:", " ".join(str(c) for c in cmd), file=sys.stderr)
        print(r.stderr[-3000:], file=sys.stderr)
        raise SystemExit(1)
    return r

def probe(path):
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)])
    return json.loads(r.stdout)

def stream_info(path):
    """duration, width, height, fps, audio hai ya nahi."""
    d = probe(path)
    fmt = d.get("format", {})
    duration = float(fmt.get("duration", 0))
    video = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if video is None:
        raise SystemExit(f"Video stream nahi mila: {path}")
    w, h = int(video["width"]), int(video["height"])
    num, den = video.get("r_frame_rate", "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return duration, w, h, fps, audio is not None

def detect_crop(path, seek=30.0, frames=48):
    """Video me kaale bars (letterbox) hain ya nahi, cropdetect se pata lagao.

    Bars poori movie me ek jaise rehte hain (aspect ratio ek hi hota
    hai), isliye pehle part par ek baar check karna kaafi hai.
    Seek part ki length ke hisaab se adjust hoti hai — chhota part
    ho to bhi kaam kare (seek ke baad kam se kam ~2 sec bache, taaki
    48 frames mil jayein).
    Returns 'w:h:x:y' ya None (agar poora frame hi picture hai, ya
    pata na chale).
    """
    dur = stream_info(path)[0]
    if dur > 0:
        seek = min(seek, max(0.5, dur - 2.0))
    cmd = ["ffmpeg", "-v", "info", "-ss", str(seek), "-i", str(path),
           "-vf", "cropdetect=limit=24:round=2:reset=0",
           "-frames:v", str(frames), "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    crops = re.findall(r"crop=(\d+:\d+:\d+:\d+)", r.stderr)
    if not crops:
        return None
    # sabse zyada baar aaya hua crop lo — kisi ek dark frame ki galat
    # reading se bachne ke liye.
    crop = Counter(crops).most_common(1)[0][0]
    w, h, x, y = (int(v) for v in crop.split(":"))
    _, vw, vh, _, _ = stream_info(path)
    if (w, h, x, y) == (vw, vh, 0, 0):
        return None
    return crop

def make_vertical_parts(parts_dir, args):
    """Har landscape part ka 9:16 vertical Reel banao.

    Poora frame beech me dikhta hai; upar-neeche usi video ka BLUR
    hota hai (koi solid color nahi, koi kaale bars nahi) — blur
    video ka hissa lagta hai, isliye Facebook "Your video has a
    border" wala flag nahi lagata. Video ke apne kaale bars
    (letterbox) pehle auto-cut ho jate hain (crop), phir blur
    lagta hai — to chalti video ke upar-neeche kaala hissa bhi nahi
    aata aur blur me bhi kaala nahi aata. Length bilkul wahi rehti
    hai (sirf dobara encode hota hai).
    """
    parts = sorted(parts_dir.glob("part_*.mp4"))
    if not parts:
        raise SystemExit(f"Parts nahi mile: {parts_dir}")
    vdir = parts_dir.with_name(parts_dir.name + "_vertical")
    vdir.mkdir(exist_ok=True)
    # Pehle part se kaale bars ka pata lagao (poori movie me ek jaise
    # hote hain) — baaki sab parts par wahi crop lagega. Crop pehle
    # lagta hai, phir blur — taaki blur me bhi kaala na aaye.
    crop = detect_crop(parts[0])
    if crop:
        print(f"  kaale bars mile — auto-cut: crop={crop}")
    for i, p in enumerate(parts):
        out_path = vdir / p.name
        # crop (kaale bars hatao, agar hain) -> do hisse:
        #  bg: crop wala frame -> 540x960 bharo -> blur -> 1080x1920
        #      (aadhi resolution par blur tez hota hai) -> halka dark
        #  fg: crop wala frame -> 1080 wide scale -> beech me overlay
        src = "[0:v]"
        if crop:
            src = f"[0:v]crop={crop},"
        filt = (f"{src}split=2[bg][fg];"
                f"[bg]scale=540:960:force_original_aspect_ratio=increase,"
                f"crop=540:960,gblur=sigma=20,scale=1080:1920,"
                f"eq=brightness=-0.08[bg2];"
                f"[fg]scale=1080:-2[fg2];"
                f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]")
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-i", str(p),
               "-filter_complex", filt,
               "-map", "[vout]", "-map", "0:a?",
               "-c:v", "libx264", "-preset", args.preset,
               "-crf", str(args.crf), "-pix_fmt", "yuv420p",
               "-c:a", "copy", str(out_path)]
        run(cmd)
        if (i + 1) % 10 == 0 or i + 1 == len(parts):
            print(f"  vertical: {i+1}/{len(parts)} done")
    total = sum(stream_info(p)[0] for p in sorted(vdir.glob("part_*.mp4")))
    print(f"Vertical Reels: {len(parts)} parts (1080x1920) -> {vdir} | kul {total:.1f} sec")
    return vdir

def split_parts(out, part_len, vertical=False, args=None, reels_only=False):
    """Final output ko lagbhag `part_len` sec ke parts me kaato.

    Fast tareeka: -c copy (dobara encode nahi), isliye kaat keyframe
    par padta hai — har part lagbhag part_len sec ka hota hai, kuch
    second upar-neeche ho sakta hai.
    reels_only ho to vertical banne ke baad 16:9 wale parts hata
    diye jate hain — sirf Reels wale parts rehte hain.
    """
    parts_dir = out.with_name(out.stem + "_parts")
    parts_dir.mkdir(exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-i", str(out),
           "-f", "segment", "-segment_time", f"{part_len:.1f}",
           "-segment_start_number", "1",
           "-reset_timestamps", "1",
           "-map", "0", "-c", "copy",
           str(parts_dir / "part_%02d.mp4")]
    run(cmd)
    parts = sorted(parts_dir.glob("part_*.mp4"))
    if not parts:
        raise SystemExit(f"Parts nahi bane: {parts_dir}")
    total = 0.0
    for p in parts:
        total += stream_info(p)[0]
    print(f"Parts: {len(parts)} parts (lagbhag {part_len:.0f}s ke) -> {parts_dir} | kul {total:.1f} sec")
    if vertical:
        make_vertical_parts(parts_dir, args)
        if reels_only:
            shutil.rmtree(parts_dir, ignore_errors=True)
            print(f"Reels-only: 16:9 wale parts hata diye, sirf vertical Reels rakhe hain -> {parts_dir.name}_vertical")
    return parts_dir


def main():
    ap = argparse.ArgumentParser(description="Reels edit: movie ke parts banao aur unke 9:16 vertical Reels banao (blur background, kaale bars auto-cut). Movie automation se alag file hai.")
    ap.add_argument("movie", help="movie file (YouTube se download ki hui, ya pehle se bani hui)")
    ap.add_argument("--parts", type=float, default=None, help="parts kitne sec ke hon (120 = 2 min; default 120)")
    ap.add_argument("--split-only", action="store_true",
                    help="file ke parts banao (dobara process nahi hoga)")
    ap.add_argument("--vertical-parts", action="store_true",
                    help="parts ke saath 9:16 vertical Reel wale parts bhi banao (upar-neeche usi video ka blur, kaale bars nahi; length wahi)")
    ap.add_argument("--reels-only", action="store_true",
                    help="sirf Reels wale (vertical) parts banao; 16:9 wale parts mat rakho (vertical-parts bhi on ho jata hai)")
    ap.add_argument("--vertical-only", action="store_true",
                    help="sirf vertical Reels banao: movie argument ek pehle se processed file hai; uske <file>_parts/ folder ke parts ko vertical me badlo")
    ap.add_argument("--crf", type=int, default=20, help="video quality, kam = behtar (default 20)")
    ap.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    args = ap.parse_args()
    if args.reels_only:
        args.vertical_parts = True

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"'{tool}' nahi mila. Pehle ffmpeg install karo: https://ffmpeg.org/download.html")

    # ---- VERTICAL-ONLY mode: pehle se bane parts ko vertical Reels me badlo
    if args.vertical_only:
        src = Path(args.movie)
        parts_dir = src.with_name(src.stem + "_parts")
        if not parts_dir.is_dir():
            raise SystemExit(f"Parts folder nahi mila: {parts_dir} (pehle --parts se parts banao)")
        make_vertical_parts(parts_dir, args)
        print(f"DONE (vertical only): {parts_dir.name}_vertical/")
        return

    # ---- SPLIT mode: file ke parts banao (aur vertical bhi, agar manga ho)
    src = Path(args.movie)
    if not src.exists():
        raise SystemExit(f"File nahi mili: {src}")
    part_len = args.parts if args.parts else 120.0
    split_parts(src, part_len, vertical=args.vertical_parts, args=args, reels_only=args.reels_only)
    print(f"DONE: {src.stem}_parts/")
    return


if __name__ == "__main__":
    main()
