#!/usr/bin/env python3
"""
Movie Automation (ffmpeg + Python)
====================================
Ek movie par ye sab automatically lagata hai:

1. SPEED  - Har 5 minute ke block me pehle 1.5 minute ki clip ko 1.04x
           speed par chalata hai (default 1.04; 1.03 bhi set kar sakte ho).
2. MIRROR - Har 5 minute ke block ke CENTER me 2 minute ka mirror
           effect (hflip: left <-> right).
3. FADE   - Mirror shuru hote hi 1.5 second fade-in aur mirror khatam
           hote hi 1.5 second fade-out (fade to black).
4. OVERLAY - Dusri clip ko poori movie par overlay karta hai:
           blend/opacity 3% aur uski audio volume 5%.
           Overlay movie ke LAST tak chalega (chhota ho to loop hoga).
5. HOURLY - Har 1 ghante me 4 baar ek hi halka sa video effect
           (saturation/brightness me halka badlav), taaki sab uniform lage.
6. PARTS  - Agar --parts 120 diya ho to final output ko 2-2 minute ke
           parts me kaat kar <output>_parts/ folder me rakh deta hai
           (part_01.mp4, part_02.mp4 ...). Full file bhi alag se rehti
           hai. Parts fast kaate jate hain (bina dobara encode), isliye
           har part lagbhag 2 minute ka hota hai — keyframe ki wajah
           se kuch second ka farak ho sakta hai.
7. SPLIT-ONLY - Agar file pehle se processed hai (jaise GitHub par
           full movie upload hone ke baad) to sirf parts banao:
           --split-only movie_processed.mp4 --parts 120
8. VERTICAL REELS - Parts ko 9:16 vertical Reel ke roop me bhi banao
           (--vertical-parts ya --vertical-only). Poora frame beech
           me dikhta hai, upar-neeche poora SOLID COLOR hota hai
           (koi blur nahi, koi kaale bars nahi) — isliye Facebook
           "border" wala flag nahi lagata. Video ke apne kaale bars
           (letterbox) bhi auto-cut ho jate hain, to chalti video ke
           upar-neeche kaala hissa bhi nahi aata. Length wahi rehti
           hai (2-2 minute), sirf shape vertical hota hai.
           --reels-only ke saath 16:9 wale parts bilkul nahi rakhe
           jate — sirf vertical Reels wale parts bante hain.
           Color badalna ho to --bg-color RRGGBB (default 4A0E0E).

Usage:
    python movie_automation.py movie.mp4 overlay.mp4
    python movie_automation.py movie.mp4 overlay.mp4 -o final.mp4
    python movie_automation.py movie.mp4 overlay.mp4 --parts 120
    python movie_automation.py --split-only movie_processed.mp4 --parts 120
    python movie_automation.py --split-only movie_processed.mp4 --parts 120 --vertical-parts
    python movie_automation.py --vertical-only movie_processed.mp4

    movie.mp4   : original movie (tumhari, rights wali)
    overlay.mp4 : overlay karne wali dusri clip (optional bhi hai:
                  --no-overlay se band kar sakte ho)

Requirements: ffmpeg + ffprobe PATH me hone chahiye (aur Python 3).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------- defaults
BLOCK = 300.0          # 5 minute ka ek block
SPEED_LEN = 90.0       # block ke shuru ke 90 sec (1.5 min) speed up
SPEED = 1.04           # speed multiplier (1.03 ya 1.04)
MIRROR_LEN = 120.0     # block ke center ke 120 sec (2 min) mirror
FADE = 1.5             # mirror ke start/end par fade (seconds)
HOUR = 3600.0          # 1 ghanta
HOURLY_AT = [600, 1500, 2400, 3300]   # har ghante ke andar 4 waqt (sec)
HOURLY_LEN = 90.0      # har hourly effect kitne second ka
HOURLY_EQ = "saturation=1.08:brightness=0.015:contrast=1.02"
OVERLAY_OPACITY = 0.03   # 3% blend
OVERLAY_VOLUME = 0.05     # 5% volume


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


def windows(duration, args):
    """Har effect ke time-windows banao: (start, end) ki list."""
    speed_w, mirror_w, hourly_w = [], [], []
    t = 0.0
    while t < duration:
        b_end = min(t + args.block, duration)
        s_end = min(t + args.speed_len, b_end)
        speed_w.append((t, s_end))
        m_start = t + args.speed_len
        m_end = min(m_start + args.mirror_len, b_end)
        if m_end > m_start:
            mirror_w.append((m_start, m_end))
        t += args.block
    h = 0.0
    while h < duration:
        for off in args.hourly_at:
            s = h + off
            e = min(s + args.hourly_len, duration)
            if s < duration and e > s:
                hourly_w.append((s, e))
        h += args.hour
    return speed_w, mirror_w, hourly_w


def in_windows(ws, x, eps=1e-6):
    return any(s - eps <= x < e - eps for s, e in ws)


def build_segments(duration, args):
    """Saare cut-points se chhote tukde banao; har tukde ke effects tay karo."""
    speed_w, mirror_w, hourly_w = windows(duration, args)
    cuts = {0.0, duration}
    for ws in (speed_w, mirror_w, hourly_w):
        for s, e in ws:
            cuts.add(round(s, 3))
            cuts.add(round(e, 3))
    cuts = sorted(c for c in cuts if 0.0 <= c <= duration)
    segs = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < 0.05:
            continue
        is_speed = in_windows(speed_w, a)
        is_mirror = in_windows(mirror_w, a)
        is_hourly = in_windows(hourly_w, a)
        # fade sirf mirror-run ke bilkul start aur bilkul end par
        fade_in = is_mirror and any(abs(a - s) < 1e-3 for s, _ in mirror_w)
        fade_out = is_mirror and any(abs(b - e) < 1e-3 for _, e in mirror_w)
        segs.append({
            "start": a, "end": b, "dur": b - a,
            "speed": is_speed, "mirror": is_mirror, "hourly": is_hourly,
            "fade_in": fade_in, "fade_out": fade_out,
        })
    return segs


def vf_chain(seg, args, w, h, fps):
    """Ek tukde ke liye video filter chain."""
    out_dur = seg["dur"] / args.speed if seg["speed"] else seg["dur"]
    parts = []
    if seg["speed"]:
        parts.append(f"setpts=(PTS-STARTPTS)/{args.speed}")
    else:
        parts.append("setpts=PTS-STARTPTS")
    if seg["mirror"]:
        parts.append("hflip")
    if seg["fade_in"]:
        d = min(args.fade, out_dur * 0.4)
        parts.append(f"fade=t=in:st=0:d={d:.2f}")
    if seg["fade_out"]:
        d = min(args.fade, out_dur * 0.4)
        parts.append(f"fade=t=out:st={out_dur - d:.2f}:d={d:.2f}")
    if seg["hourly"]:
        parts.append(f"eq={args.hourly_eq}")
    parts.append(f"fps={fps:.3f}")
    parts.append(f"scale={w}:{h}")
    parts.append("format=yuv420p")
    return ",".join(parts), out_dur


def detect_crop(path, seek=30.0, frames=48):
    """Video me kaale bars (letterbox) hain ya nahi, cropdetect se pata lagao.

    Bars poori movie me ek jaise rehte hain (aspect ratio ek hi hota
    hai), isliye pehle part par ek baar check karna kaafi hai.
    Returns 'w:h:x:y' ya None (agar poora frame hi picture hai, ya
    pata na chale).
    """
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

    Poora frame beech me dikhta hai; upar-neeche poora SOLID COLOR
    hota hai (koi blur nahi, koi kaale bars nahi) — isliye Facebook
    "Your video has a border" wala flag nahi lagata. Video ke apne
    kaale bars (letterbox) bhi auto-cut ho jate hain, to chalti video
    ke upar-neeche kaala hissa bhi nahi aata. Length bilkul wahi
    rehti hai (sirf dobara encode hota hai).
    """
    parts = sorted(parts_dir.glob("part_*.mp4"))
    if not parts:
        raise SystemExit(f"Parts nahi mile: {parts_dir}")
    vdir = parts_dir.with_name(parts_dir.name + "_vertical")
    vdir.mkdir(exist_ok=True)
    # Pehle part se kaale bars ka pata lagao (poori movie me ek jaise
    # hote hain) — baaki sab parts par wahi crop lagega.
    crop = detect_crop(parts[0])
    if crop:
        print(f"  kaale bars mile — auto-cut: crop={crop}")
    bg = (getattr(args, "bg_color", None) or "4A0E0E").lstrip("#")
    if len(bg) != 6 or any(c not in "0123456789abcdefABCDEF" for c in bg):
        bg = "4A0E0E"
    for i, p in enumerate(parts):
        out_path = vdir / p.name
        # crop (kaale bars hatao, agar hain) -> 1080 wide scale ->
        # 1080x1920 solid-color canvas ke beech me rakh do (pad).
        chain = ""
        if crop:
            chain += f"crop={crop},"
        chain += (f"scale=1080:-2,"
                  f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x{bg},"
                  f"format=yuv420p[vout]")
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-i", str(p),
               "-filter_complex", f"[0:v]{chain}",
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
    ap = argparse.ArgumentParser(description="Movie automation: speed + mirror + fade + overlay + hourly effects")
    ap.add_argument("movie", help="original movie file")
    ap.add_argument("overlay", nargs="?", default=None, help="overlay clip (optional)")
    ap.add_argument("-o", "--output", default=None, help="output file (default: <movie>_processed.mp4)")
    ap.add_argument("--block", type=float, default=BLOCK, help="block length sec (default 300 = 5 min)")
    ap.add_argument("--speed-len", type=float, default=SPEED_LEN, help="speed segment sec (default 90)")
    ap.add_argument("--speed", type=float, default=SPEED, help="speed multiplier (default 1.04)")
    ap.add_argument("--mirror-len", type=float, default=MIRROR_LEN, help="mirror segment sec (default 120)")
    ap.add_argument("--fade", type=float, default=FADE, help="fade sec at mirror start/end (default 1.5)")
    ap.add_argument("--hour", type=float, default=HOUR, help="hour length sec (default 3600)")
    ap.add_argument("--hourly-at", type=float, nargs="+", default=HOURLY_AT,
                    help="hour ke andar effect ke waqt, sec me (default 600 1500 2400 3300)")
    ap.add_argument("--hourly-len", type=float, default=HOURLY_LEN, help="hourly effect length sec (default 90)")
    ap.add_argument("--hourly-eq", default=HOURLY_EQ, help="hourly effect (eq filter settings)")
    ap.add_argument("--opacity", type=float, default=OVERLAY_OPACITY, help="overlay blend 0 to 1 (default 0.03)")
    ap.add_argument("--overlay-volume", type=float, default=OVERLAY_VOLUME, help="overlay audio volume 0 to 1 (default 0.05)")
    ap.add_argument("--crf", type=int, default=20, help="video quality, kam = behtar (default 20)")
    ap.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    ap.add_argument("--no-overlay", action="store_true", help="overlay mat lagao")
    ap.add_argument("--parts", type=float, default=None, help="final output ko itne sec ke parts me kaato (120 = 2 min; default off)")
    ap.add_argument("--split-only", action="store_true",
                    help="sirf parts banao: movie argument ek pehle se processed file hai (jaise movie_processed.mp4); dobara process nahi hoga")
    ap.add_argument("--vertical-parts", action="store_true",
                    help="parts ke saath 9:16 vertical Reel wale parts bhi banao (solid color background, kaale bars nahi; length wahi)")
    ap.add_argument("--bg-color", default="4A0E0E",
                    help="Reels me video ke upar-neeche ka solid color (6 hex digits, bina # ke; default 4A0E0E = gehra maroon)")
    ap.add_argument("--reels-only", action="store_true",
                    help="sirf Reels wale (vertical) parts banao; 16:9 wale parts mat rakho (vertical-parts bhi on ho jata hai)")
    ap.add_argument("--vertical-only", action="store_true",
                    help="sirf vertical Reels banao: movie argument ek pehle se processed file hai; uske <file>_parts/ folder ke parts ko vertical me badlo")
    ap.add_argument("--keep-temp", action="store_true", help="temp segment files mat hatao")
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

    # ---- SPLIT-ONLY mode: pehle se bani file ke sirf parts banao
    if args.split_only:
        src = Path(args.movie)
        if not src.exists():
            raise SystemExit(f"File nahi mili: {src}")
        part_len = args.parts if args.parts else 120.0
        split_parts(src, part_len, vertical=args.vertical_parts, args=args, reels_only=args.reels_only)
        print(f"DONE (split only): {src.stem}_parts/")
        return

    movie = Path(args.movie)
    if not movie.exists():
        raise SystemExit(f"Movie file nahi mili: {movie}")
    overlay = Path(args.overlay) if (args.overlay and not args.no_overlay) else None
    if overlay is not None and not overlay.exists():
        raise SystemExit(f"Overlay file nahi mili: {overlay}")

    out = Path(args.output) if args.output else movie.with_name(movie.stem + "_processed.mp4")

    duration, w, h, fps, has_audio = stream_info(movie)
    print(f"Movie: {movie} | {duration:.1f} sec ({duration/60:.1f} min) | {w}x{h} | {fps:.2f} fps | audio={'yes' if has_audio else 'no'}")
    if overlay is not None:
        od, _, _, _, o_audio = stream_info(overlay)
        print(f"Overlay: {overlay} | {od:.1f} sec | audio={'yes' if o_audio else 'no'} | opacity={args.opacity*100:.1f}% | volume={args.overlay_volume*100:.0f}%")
    print(f"Plan: har {args.block:.0f}s block me {args.speed_len:.0f}s @ {args.speed}x speed + "
          f"{args.mirror_len:.0f}s mirror (fade {args.fade}s) + har ghante me {len(args.hourly_at)}x effect")

    segs = build_segments(duration, args)
    exp_dur = sum(s["dur"] / args.speed if s["speed"] else s["dur"] for s in segs)
    print(f"Segments: {len(segs)} | expected output duration: {exp_dur:.1f} sec")

    tmp = Path(tempfile.mkdtemp(prefix="movie-auto-"))
    try:
        # ---- STEP 1: har tukda alag encode karo
        list_path = tmp / "list.txt"
        with open(list_path, "w") as lf:
            for i, seg in enumerate(segs):
                vf, _ = vf_chain(seg, args, w, h, fps)
                seg_file = tmp / f"seg_{i:04d}.mp4"
                cmd = ["ffmpeg", "-y", "-v", "error",
                       "-ss", f"{seg['start']:.3f}", "-to", f"{seg['end']:.3f}",
                       "-i", str(movie),
                       "-map", "0:v:0", "-filter:v", vf,
                       "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
                       "-pix_fmt", "yuv420p"]
                if has_audio:
                    af = f"asetpts=PTS-STARTPTS,atempo={args.speed}" if seg["speed"] else "asetpts=PTS-STARTPTS"
                    cmd += ["-map", "0:a:0", "-filter:a", af,
                            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
                else:
                    cmd += ["-an"]
                cmd.append(str(seg_file))
                run(cmd)
                lf.write(f"file '{seg_file}'\n")
                if (i + 1) % 20 == 0 or i + 1 == len(segs):
                    print(f"  segments: {i+1}/{len(segs)} done")
        # ---- STEP 2: tukde jodo
        base = tmp / "base.mp4"
        r = run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(list_path), "-c", "copy", str(base)], check=False)
        if r.returncode != 0:
            print("  concat (copy) fail hua, re-encode karke jod raha hun...")
            run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                 "-i", str(list_path), "-c:v", "libx264", "-preset", args.preset,
                 "-crf", str(args.crf), "-c:a", "aac", "-b:a", "160k", str(base)])
        base_dur = stream_info(base)[0]
        print(f"Base (speed+mirror+fade+hourly) ready: {base_dur:.1f} sec")

        # ---- STEP 3: overlay (poori movie ke last tak)
        if overlay is None:
            shutil.copy(base, out)
            print(f"DONE (bina overlay): {out}")
        else:
            o_audio = stream_info(overlay)[4]
            fc = [f"[1:v]scale={w}:{h},format=yuva420p,colorchannelmixer=aa={args.opacity}[ov]",
                  f"[0:v][ov]overlay=0:0,format=yuv420p[vout]"]
            maps = ["-map", "[vout]"]
            audio_opts: list = []
            if has_audio and o_audio:
                # overlay ki audio 5% volume par main audio ke saath mix
                fc.append(f"[1:a]volume={args.overlay_volume}[aov]")
                fc.append("[0:a][aov]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")
                maps += ["-map", "[aout]"]
                audio_opts = ["-c:a", "aac", "-b:a", "160k"]
            elif has_audio:
                maps += ["-map", "0:a"]
                audio_opts = ["-c:a", "aac", "-b:a", "160k"]
            else:
                audio_opts = ["-an"]
            cmd = (["ffmpeg", "-y", "-v", "error",
                    "-i", str(base), "-stream_loop", "-1", "-i", str(overlay),
                    "-filter_complex", ";".join(fc)]
                   + maps
                   + ["-c:v", "libx264", "-preset", args.preset,
                      "-crf", str(args.crf), "-pix_fmt", "yuv420p"]
                   + audio_opts
                   + ["-t", f"{base_dur:.3f}", str(out)])
            run(cmd)
        print(f"DONE: {out} | duration {stream_info(out)[0]:.1f} sec")

        # ---- STEP 4: 2-2 minute ke parts (agar --parts diya ho)
        if args.parts:
            split_parts(out, args.parts, vertical=args.vertical_parts, args=args, reels_only=args.reels_only)
    finally:
        if args.keep_temp:
            print(f"Temp files rakhe hain: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
