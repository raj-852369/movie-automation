#!/usr/bin/env python3
"""
song_detect.py — Movie me gaane (music segments) dhoondho.

Kaam: movie ka SIRF audio download karo (poori video nahi), phir har
1-second window me check karo ke wahan music hai ya nahi, aur music
wale hisson ki timing list de do.

Usage:
  python3 song_detect.py --url <youtube-link> [--cookies cookies.txt]
  python3 song_detect.py --audio movie_audio.m4a
  python3 song_detect.py --audio test.wav --out song_times.txt

Output: terminal me saaf list + --out file (default song_times.txt):
  1. 00:12:30 - 00:15:40 (3m10s)
  Cut format (aage --cut ke liye): 12:30-15:40,45:00-48:20

Method (halka, koi badi download nahi — sirf ffmpeg + numpy):
  Audio ko 16kHz mono me decode karke 1s windows (0.5s hop) me
  spectral flatness + energy nikalo. Tonal music ki flatness LOW
  hoti hai (0.05-0.25), speech/noise ki HIGH (0.3-0.9). Isliye
  flatness < threshold + awaaz hai = music window. Phir chhote
  gaps bharo, chhote tukde hatao.

IMPORTANT: ye "music" dhoondhta hai, "gaana" nahi — background
score bhi music hi hai. List me jo segments aayen, user khud
bataega kaunse gaane hain, phir main workflow --cut se woh kaatega.
Isliye recall zyada rakha hai (gaana chhootna nahi chahiye);
extra entries (score) user ki confirm se nikal jayenge.
"""

import argparse
import os
import subprocess
import sys

import numpy as np

SR = 16000          # sample rate for analysis
WIN_SEC = 1.0       # window length
HOP_SEC = 0.5       # hop between windows
FLAT_THRESH = 0.30  # spectral flatness below this = tonal (music-like)
SILENCE_RMS = 0.005  # below this = silence, not music


def fmt_time(sec):
    """seconds -> HH:MM:SS ya MM:SS."""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%02d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def fmt_dur(sec):
    sec = int(round(sec))
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return "%dh%02dm" % (h, m)
    if m:
        return "%dm%02ds" % (m, s)
    return "%ds" % s


def download_audio(url, cookies=None, out="movie_audio"):
    """yt-dlp se SIRF audio download karo (video nahi).

    Main workflow wala proven pattern: pehle EJS solver ke saath,
    fail ho to android client se dobara try karo (YouTube ke
    bot-check / n-challenge ke liye).
    """
    base = ["yt-dlp", "--no-playlist", "--retries", "3",
            "--remote-components", "ejs:github",
            "-f", "bestaudio", "-o", out + ".%(ext)s"]
    if cookies and os.path.exists(cookies):
        base += ["--cookies", cookies]
    print("audio download ho raha hai (sirf audio, video nahi)...", flush=True)
    r = subprocess.run(base + [url], capture_output=True, text=True)
    if r.returncode != 0:
        print("Pehla tareeka fail — android client se dobara try kar raha hoon...",
              flush=True)
        r2 = subprocess.run(
            base + ["--extractor-args", "youtube:player_client=android", url],
            capture_output=True, text=True)
        if r2.returncode != 0:
            print(r.stdout[-2000:] if r.stdout else "")
            print(r.stderr[-2000:] if r.stderr else "", file=sys.stderr)
            raise SystemExit("audio download fail ho gaya (link/cookies check karo).")
    # jo file bani, use dhoondho
    for f in os.listdir("."):
        if f.startswith(out + ".") and os.path.isfile(f):
            return f
    raise SystemExit("audio file nahi mili.")


def window_features(buf):
    """1s mono int16 buffer -> (rms, flatness, zcr)."""
    x = buf.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
    if rms < 1e-6:
        return rms, 1.0, 0.0
    # zero crossing rate
    zcr = float(np.mean(np.abs(np.diff(np.sign(x))))) / 2.0
    # spectral flatness, 100Hz-8kHz band
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / SR)
    band = (freqs >= 100) & (freqs <= 8000)
    p = spec[band] + 1e-12
    flat = float(np.exp(np.mean(np.log(p))) / np.mean(p))
    return rms, flat, zcr


def median_filter(labels, k=5):
    """Simple median filter (0/1 labels) — flicker hatane ke liye."""
    n = len(labels)
    out = np.zeros(n, dtype=int)
    half = k // 2
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = 1 if np.median(labels[lo:hi]) >= 0.5 else 0
    return out


def detect(audio_path, flat_thresh=FLAT_THRESH, min_len=20.0,
           merge_gap=10.0):
    """Audio file -> list of (start_sec, end_sec) music segments."""
    cmd = ["ffmpeg", "-v", "error", "-i", audio_path,
           "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    win_samples = int(WIN_SEC * SR)
    hop_samples = int(HOP_SEC * SR)
    buf = np.zeros(0, dtype=np.int16)
    labels = []          # 1 = music window, 0 = nahi
    total_sec = 0.0
    while True:
        chunk = proc.stdout.read(hop_samples * 2)  # 0.5s int16
        if not chunk:
            break
        new = np.frombuffer(chunk, dtype=np.int16)
        buf = np.concatenate([buf, new]) if len(buf) else new
        total_sec += len(new) / SR
        # jab tak 1s ka buffer na ho, wait karo
        while len(buf) >= win_samples:
            w = buf[:win_samples]
            buf = buf[hop_samples:]
            rms, flat, zcr = window_features(w)
            is_music = 1 if (rms >= SILENCE_RMS and flat < flat_thresh) else 0
            labels.append(is_music)
    proc.wait()
    if proc.returncode != 0:
        raise SystemExit("ffmpeg audio decode fail ho gaya.")
    if not labels:
        return [], total_sec

    labels = median_filter(np.array(labels, dtype=int), k=5)

    # consecutive music windows -> segments (window i covers i*hop .. i*hop+win)
    segments = []
    i = 0
    n = len(labels)
    while i < n:
        if labels[i]:
            j = i
            while j + 1 < n and labels[j + 1]:
                j += 1
            start = i * HOP_SEC
            end = j * HOP_SEC + WIN_SEC
            segments.append([start, end])
            i = j + 1
        else:
            i += 1

    # chhote gaps bharo
    merged = []
    for s in segments:
        if merged and s[0] - merged[-1][1] <= merge_gap:
            merged[-1][1] = s[1]
        else:
            merged.append(s)

    # chhote tukde hatao
    segments = [s for s in merged if (s[1] - s[0]) >= min_len]
    return segments, total_sec


def main():
    ap = argparse.ArgumentParser(
        description="Movie ke gaano (music segments) ki timing nikalo — sirf audio se.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--url", help="YouTube link (sirf audio download hoga)")
    g.add_argument("--audio", help="Local audio/video file")
    ap.add_argument("--cookies", default=None,
                    help="cookies.txt (yt-dlp ke liye, optional)")
    ap.add_argument("--out", default="song_times.txt",
                    help="timings file (default song_times.txt)")
    ap.add_argument("--min-len", type=float, default=20.0,
                    help="isse chhota music tukda list me nahi aayega (sec, default 20)")
    ap.add_argument("--merge-gap", type=float, default=10.0,
                    help="isse chhota gap bhar diya jayega (sec, default 10)")
    ap.add_argument("--flat-thresh", type=float, default=FLAT_THRESH,
                    help="spectral flatness threshold (default 0.30; kam = sakht)")
    args = ap.parse_args()

    audio_path = args.audio
    downloaded = None
    if args.url:
        downloaded = download_audio(args.url, args.cookies)
        audio_path = downloaded

    print("analyse ho raha hai: %s" % audio_path, flush=True)
    segments, total = detect(audio_path, args.flat_thresh,
                             args.min_len, args.merge_gap)

    lines = []
    lines.append("==================== SONG TIMES ====================")
    lines.append("Audio length: %s" % fmt_time(total))
    if not segments:
        lines.append("Koi music segment nahi mila (min %ds)." % int(args.min_len))
        lines.append("Ya to movie me gaane nahi hain, ya audio bahut halka hai.")
    else:
        for idx, (s, e) in enumerate(segments, 1):
            lines.append("%d. %s - %s (%s)" % (
                idx, fmt_time(s), fmt_time(e), fmt_dur(e - s)))
        cut = ",".join("%s-%s" % (fmt_time(s), fmt_time(e))
                       for s, e in segments)
        lines.append("Cut format (aage --cut ke liye):")
        lines.append(cut)
    lines.append("====================================================")
    lines.append("NOTE: ye MUSIC wale hisse hain (gaane + background")
    lines.append("score dono). Kaunse gaane hain, ye aap confirm karo,")
    lines.append("phir main workflow unhe --cut se kaat dega.")
    text = "\n".join(lines) + "\n"
    print(text, flush=True)
    with open(args.out, "w") as f:
        f.write(text)
    print("saved: %s" % args.out, flush=True)
    if downloaded:
        try:
            os.remove(downloaded)
        except OSError:
            pass


if __name__ == "__main__":
    main()
