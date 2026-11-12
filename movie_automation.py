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
           blend/opacity 3.5% aur uski audio volume 5%.
           Overlay movie ke LAST tak chalega (chhota ho to loop hoga).
5. HOURLY - Har 1 ghante me 4 baar ek hi halka sa video effect
           (saturation/brightness me halka badlav), taaki sab uniform lage.

Usage:
    python movie_automation.py movie.mp4 overlay.mp4
    python movie_automation.py movie.mp4 overlay.mp4 -o final.mp4

    movie.mp4   : original movie (tumhari, rights wali)
    overlay.mp4 : overlay karne wali dusri clip (optional bhi hai:
                  --no-overlay se band kar sakte ho)

Requirements: ffmpeg + ffprobe PATH me hone chahiye (aur Python 3).
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
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
OVERLAY_OPACITY = 0.035   # 3.5% blend
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
    ap.add_argument("--opacity", type=float, default=OVERLAY_OPACITY, help="overlay blend 0 to 1 (default 0.035)")
    ap.add_argument("--overlay-volume", type=float, default=OVERLAY_VOLUME, help="overlay audio volume 0 to 1 (default 0.05)")
    ap.add_argument("--crf", type=int, default=20, help="video quality, kam = behtar (default 20)")
    ap.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    ap.add_argument("--no-overlay", action="store_true", help="overlay mat lagao")
    ap.add_argument("--keep-temp", action="store_true", help="temp segment files mat hatao")
    args = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"'{tool}' nahi mila. Pehle ffmpeg install karo: https://ffmpeg.org/download.html")

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
            return
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
    finally:
        if args.keep_temp:
            print(f"Temp files rakhe hain: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
