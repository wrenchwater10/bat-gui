#!/usr/bin/env python3
"""
VIDEO TOOLKIT  -  a GUI for ffmpeg
Quality match, presets, custom settings, converters, audio tools,
video/audio destroyers, datamosh, saved presets, batch queue.

Needs: Python 3.9+ (tkinter is included on Windows) and ffmpeg + ffprobe
(in PATH, or put ffmpeg.exe / ffprobe.exe next to this file / the .exe).
Optional: pip install tkinterdnd2   (enables drag and drop onto the queue)
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except Exception:
    HAS_DND = False

# ====================================================================
#  Paths, settings
# ====================================================================
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CFG_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "exported videos")
CFG_FILE = os.path.join(CFG_DIR, "video_toolkit_config.json")
PRESET_DIR = os.path.join(CFG_DIR, "_my_presets")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".wmv", ".3gp", ".ts"}
AUDIO_EXT = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

DEFAULT_SETTINGS = {
    "out_dir": CFG_DIR,
    "encoder": "libx264",
    "speed": "medium",
    "overwrite": False,
    "open_after": True,
    "show_cmd": False,
    "vd_keepfps": False,
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(CFG_FILE, "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        os.makedirs(CFG_DIR, exist_ok=True)
        with open(CFG_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception:
        pass


def find_tool(name):
    p = shutil.which(name)
    if p:
        return p
    for cand in (name + ".exe", name):
        c = os.path.join(APP_DIR, cand)
        if os.path.exists(c):
            return c
    return None


def open_path(path, select=False):
    try:
        if sys.platform.startswith("win"):
            if select and os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path if os.path.isdir(path) else os.path.dirname(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path if os.path.isdir(path) else os.path.dirname(path)])
        else:
            subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
    except Exception:
        pass


# ====================================================================
#  ffprobe helpers
# ====================================================================
def probe_json(path):
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        return None
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
            capture_output=True, text=True, creationflags=NO_WINDOW, encoding="utf-8", errors="replace",
        )
        return json.loads(r.stdout)
    except Exception:
        return None


def get_duration(path):
    d = probe_json(path)
    try:
        return float(d["format"]["duration"])
    except Exception:
        return None


def parse_time(s):
    """'90', '1:30', '00:01:30.5' -> seconds (float) or None."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        parts = [float(p) for p in s.split(":")]
    except ValueError:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def read_reference(path):
    """Return a dict of specs from a reference video (for Quality Match)."""
    d = probe_json(path)
    if not d:
        raise ValueError("Could not read that file with ffprobe.")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise ValueError("No video stream found in that file.")
    fps = v.get("r_frame_rate", "30/1")
    try:
        n, dd = fps.split("/")
        fps = n if int(dd) == 1 else fps
    except Exception:
        fps = "30"
    vb = v.get("bit_rate")
    if not vb or vb == "N/A":
        vb = d.get("format", {}).get("bit_rate", "2000000")
    spec = {
        "w": str(v.get("width", 1280)), "h": str(v.get("height", 720)), "fps": fps, "vb": str(vb),
        "ab": "128000", "ar": "44100", "ac": "2",
    }
    if a:
        spec["ar"] = str(a.get("sample_rate", "44100"))
        spec["ac"] = str(a.get("channels", 2))
        if a.get("bit_rate") and a.get("bit_rate") != "N/A":
            spec["ab"] = str(a["bit_rate"])
    return spec


# ====================================================================
#  Validation helpers
# ====================================================================
def need(value, pattern, label):
    value = (value or "").strip()
    if not re.fullmatch(pattern, value):
        raise ValueError(f"{label} doesn't look right: '{value}'")
    return value


RE_INT = r"-?\d+"
RE_NUM = r"\d+(\.\d+)?"
RE_FPS = r"\d+(\.\d+)?(/\d+)?"
RE_BR = r"\d+(\.\d+)?[kKmM]?"
RE_SNUM = r"-?\d+(\.\d+)?"


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*]', "_", s).strip()


# ====================================================================
#  Job building.  A "job" is a plain dict (see state_to_job, BUILTINS...)
#    kind: enc | tool | mosh | mux
# ====================================================================
RES_MODES = ["Fit + black bars", "Stretch to fill", "Crop / zoom to fill"]


def default_state():
    return {
        "use_res": 1, "use_fps": 1, "use_vb": 1, "use_ab": 1, "use_ar": 1, "mute": 0,
        "w": "1280", "h": "720", "resmode": RES_MODES[0], "fps": "30", "vb": "2000k",
        "ab": "128k", "ar": "44100", "ac": "2", "ss": "", "t": "", "vf_extra": "", "af_extra": "",
    }


def state_to_job(st, tag, folder):
    job = {"kind": "enc", "tag": tag, "folder": folder, "ext": "mp4"}
    vf = []
    if st["use_res"]:
        w = need(st["w"], RE_INT, "Width")
        h = need(st["h"], RE_INT, "Height")
        m = st["resmode"]
        if m == RES_MODES[0]:
            vf.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                      f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        elif m == RES_MODES[1]:
            vf.append(f"scale={w}:{h},setsar=1")
        else:
            vf.append(f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1")
    if st.get("vf_extra", "").strip():
        vf.append(st["vf_extra"].strip())
    job["vf"] = ",".join(vf)
    if st["use_fps"]:
        job["fps"] = need(st["fps"], RE_FPS, "Frame rate")
    if st["use_vb"]:
        job["vb"] = need(st["vb"], RE_BR, "Video bitrate")
    if st["use_ab"]:
        job["ab"] = need(st["ab"], RE_BR, "Audio bitrate")
    if st["use_ar"]:
        job["ar"] = need(st["ar"], r"\d+", "Audio sample rate")
        job["ac"] = need(st["ac"], r"\d+", "Audio channels")
    job["mute"] = bool(st["mute"])
    job["af"] = (st.get("af_extra") or "").strip()
    for k in ("ss", "t"):
        val = (st.get(k) or "").strip()
        if val:
            if parse_time(val) is None:
                raise ValueError(f"Trim time doesn't look right: '{val}'")
            job[k] = val
    return job


def luis_bitrate(duration):
    """Bitrate (in k) for the luisgamercool23 preset, scaled by frame count."""
    if not duration:
        return 1012
    frames = int(duration * 1000) * 30 // 1000
    k = 1012 if frames >= 2419 else 1012 * frames // 2419
    return max(k, 100)


def build_commands(job, inp, out, S, ffmpeg, dur):
    """Return (list_of_commands, temp_files_to_delete)."""
    head = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]
    trim = []
    if job.get("ss"):
        trim += ["-ss", job["ss"]]
    if job.get("t"):
        trim += ["-t", job["t"]]
    kind = job["kind"]

    if kind == "tool":
        return [head + trim + ["-i", inp] + job["targs"] + [out]], []

    if kind == "mosh":
        t1 = os.path.join(tempfile.gettempdir(), "vqt_mosh1.avi")
        t2 = os.path.join(tempfile.gettempdir(), "vqt_mosh2.avi")
        c1 = head + trim + ["-i", inp, "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-r", "30",
                            "-c:v", "mpeg4", "-q:v", "4", "-g", "30", "-bf", "0",
                            "-sc_threshold", "1000000000", "-c:a", "libmp3lame", "-b:a", "128k", t1]
        c2 = head + ["-i", t1, "-c", "copy", "-bsf:v", "noise=" + job["bsf"], t2]
        c3 = head + ["-i", t2, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
                     "-r", "30", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out]
        return [c1, c2, c3], [t1, t2]

    # kind == "enc"
    enc = S["encoder"]
    cmd = head + trim + ["-i", inp]
    if job.get("vf"):
        cmd += ["-vf", job["vf"]]
    if job.get("fps"):
        cmd += ["-r", str(job["fps"])]
    cmd += ["-c:v", enc, "-pix_fmt", "yuv420p"]
    if enc in ("libx264", "libx265"):
        cmd += ["-preset", S["speed"]]
    if enc == "libx265":
        cmd += ["-tag:v", "hvc1"]
    vb = job.get("vb")
    if job.get("luis"):
        vb = f"{luis_bitrate(dur)}k"
    if vb:
        cmd += ["-b:v", vb, "-maxrate", vb, "-bufsize", vb]
    elif enc == "libx264":
        cmd += ["-crf", "20"]
    elif enc == "libx265":
        cmd += ["-crf", "24"]
    else:
        cmd += ["-b:v", "8M"]
    if job.get("mute"):
        cmd += ["-an"]
    else:
        cmd += ["-c:a", "aac"]
        if job.get("ar"):
            cmd += ["-ar", str(job["ar"])]
        if job.get("ac"):
            cmd += ["-ac", str(job["ac"])]
        if job.get("ab"):
            cmd += ["-b:a", job["ab"]]
        if job.get("af"):
            cmd += ["-af", job["af"]]
    cmd += ["-movflags", "+faststart", out]
    return [cmd], []


def unique_path(folder, base, ext, overwrite):
    os.makedirs(folder, exist_ok=True)
    out = os.path.join(folder, f"{base}.{ext}")
    if overwrite:
        return out
    k = 0
    while os.path.exists(out):
        k += 1
        out = os.path.join(folder, f"{base}_{k}.{ext}")
    return out


# ====================================================================
#  Built-in presets / levels
# ====================================================================
def _enc(tag, desc, vf="", fps=None, vb=None, ab=None, ar=None, ac=None, af="", **extra):
    job = {"kind": "enc", "tag": tag, "folder": f"Presets/{tag}", "ext": "mp4", "vf": vf, "af": af}
    for k, v in (("fps", fps), ("vb", vb), ("ab", ab), ("ar", ar), ("ac", ac)):
        if v:
            job[k] = v
    job.update(extra)
    return {"desc": desc, "job": job}


BUILTINS = {
    "Potato": _enc("potato", "240p, 15fps, tiny bitrate, crunchy audio",
                   "scale=-2:240", "15", "100k", "24k", "22050", "1"),
    "Mega Potato": _enc("megapotato", "144p, 10fps, ~40k bitrate, 8kHz mono audio",
                        "scale=-2:144", "10", "40k", "12k", "8000", "1", "highpass=f=200,lowpass=f=3400"),
    "Deep Fried": _enc("deepfried", "Pixelated, oversaturated, noisy, bass-boosted distorted audio",
                       "scale=trunc(iw/16)*2:trunc(ih/16)*2,scale=iw*8:ih*8:flags=neighbor,"
                       "eq=contrast=1.6:saturation=3:brightness=0.05,unsharp=5:5:2.0,noise=alls=25:allf=t+u",
                       "12", "200k", "32k", "22050", "1", "bass=g=15,volume=6,acrusher=bits=8:mix=0.9"),
    "Old VHS": _enc("vhs", "Soft, noisy, washed-out old tape look",
                    "scale=-2:240,eq=saturation=1.5:contrast=1.1,noise=alls=18:allf=t,gblur=sigma=0.8,"
                    "scale=-2:480:flags=bilinear",
                    "24", "400k", "64k", "22050", "1", "highpass=f=120,lowpass=f=5000"),
    "Earrape (LOUD)": _enc("earrape", "Video kept, audio crushed and extremely loud",
                           ab="128k", af="volume=30,acrusher=bits=4:mix=0.7", warn="loud"),
    "luisgamercool23 quality": _enc(
        "luisgamercool23_quality",
        "962x720, 30fps, 128k stereo audio. Bitrate gets lower for shorter videos so they look as bad as the original.",
        "scale=962:720,setsar=1", "30", "1012k", "128k", "44100", "2", luis=True),
    "Datamosh - Classic": {"desc": "Remove ALL I-frames (maximum smear). Experimental.",
                           "job": {"kind": "mosh", "tag": "datamosh", "folder": "Presets/datamosh",
                                   "ext": "mp4", "bsf": "drop=key*n"}},
    "Datamosh - Gentle": {"desc": "Keep one I-frame about every 10 seconds. Experimental.",
                          "job": {"kind": "mosh", "tag": "datamosh_gentle", "folder": "Presets/datamosh_gentle",
                                  "ext": "mp4", "bsf": "drop=key*n*(n-300*floor(n/300))"}},
    "Corrupt (random bitstream damage)": {"desc": "Random corruption. Lower 'amount' = more damage. Can look broken.",
                                          "job": {"kind": "mosh", "tag": "corrupt", "folder": "Presets/corrupt",
                                                  "ext": "mp4", "bsf": "amount=1500"}},
}

VD_LEVELS = {
    "Bad":             dict(h="360", fps="24", vb="400k", ab="64k", ar="22050", ac="1", noise="0", af=""),
    "Worse":           dict(h="240", fps="15", vb="150k", ab="32k", ar="16000", ac="1", noise="0", af="lowpass=f=5000"),
    "Terrible":        dict(h="160", fps="12", vb="60k", ab="16k", ar="11025", ac="1", noise="10",
                            af="lowpass=f=4000,acrusher=bits=8:mix=0.9"),
    "Unwatchable":     dict(h="120", fps="8", vb="25k", ab="8k", ar="8000", ac="1", noise="20",
                            af="highpass=f=300,lowpass=f=3000,acrusher=bits=6:mix=1"),
    "Max Destruction": dict(h="96", fps="5", vb="10k", ab="8k", ar="8000", ac="1", noise="25",
                            af="highpass=f=300,lowpass=f=2500,volume=4,acrusher=bits=4:mix=1:samples=6"),
    "Custom":          dict(h="240", fps="15", vb="100k", ab="16k", ar="11025", ac="1", noise="10",
                            af="acrusher=bits=8:mix=1"),
}

AD_LEVELS = {
    "Bad":             dict(br="48k", ar="22050", ac="1", af=""),
    "Worse":           dict(br="24k", ar="16000", ac="1", af="lowpass=f=5000"),
    "Terrible":        dict(br="16k", ar="11025", ac="1", af="lowpass=f=4000,acrusher=bits=8:mix=0.9"),
    "Unlistenable":    dict(br="8k", ar="8000", ac="1", af="highpass=f=300,lowpass=f=3000,acrusher=bits=6:mix=1"),
    "Max Destruction": dict(br="8k", ar="8000", ac="1",
                            af="highpass=f=300,lowpass=f=2500,volume=4,acrusher=bits=4:mix=1:samples=6"),
    "Custom":          dict(br="16k", ar="11025", ac="1", af="acrusher=bits=8:mix=1"),
}

AUDIO_FORMATS = {  # name: (ext, codec args, default bitrate or None for lossless)
    "MP3": ("mp3", ["-c:a", "libmp3lame"], "192k"),
    "WAV (lossless)": ("wav", ["-c:a", "pcm_s16le"], None),
    "FLAC (lossless)": ("flac", ["-c:a", "flac"], None),
    "M4A (AAC)": ("m4a", ["-c:a", "aac"], "192k"),
    "OGG": ("ogg", ["-c:a", "libvorbis"], "192k"),
    "OPUS": ("opus", ["-c:a", "libopus"], "128k"),
}


# ====================================================================
#  Runner (background thread that executes ffmpeg)
# ====================================================================
class Runner(threading.Thread):
    def __init__(self, files, job, S, q, ffmpeg, outname=None):
        super().__init__(daemon=True)
        self.files, self.job, self.S, self.q, self.ffmpeg = files, job, S, q, ffmpeg
        self.outname = outname
        self.cancel = False
        self.proc = None

    def log(self, text):
        self.q.put(("log", text))

    def stop(self):
        self.cancel = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def run(self):
        ok = fail = 0
        last = None
        n = len(self.files)
        for i, f in enumerate(self.files):
            if self.cancel:
                break
            try:
                last = self.process(i, n, f)
                ok += 1
            except Exception as e:
                if self.cancel:
                    break
                fail += 1
                self.log(f"FAILED: {e}")
        self.q.put(("done", ok, fail, last, self.cancel))

    def process(self, i, n, f):
        job, S = self.job, self.S
        if job["kind"] == "mux":
            video, audio = f
            src = video
            base = f"{safe_name(os.path.splitext(os.path.basename(video))[0])}_newaudio"
        else:
            src = f
            stem = safe_name(os.path.splitext(os.path.basename(f))[0])
            base = (self.outname if (self.outname and n == 1) else f"{stem}_{job['tag']}")
        ext = job.get("ext", "mp4")
        if ext == "same":
            ext = os.path.splitext(src)[1].lstrip(".") or "mp4"
        folder = os.path.join(S["out_dir"], *job["folder"].split("/"))
        out = unique_path(folder, base, ext, S["overwrite"])
        name = os.path.basename(src)
        self.q.put(("file", f"[{i + 1}/{n}] {name}"))
        self.log(f"--- [{i + 1}/{n}] {name}")

        dur = get_duration(src)
        eff = dur
        if eff:
            ss = parse_time(job.get("ss"))
            t = parse_time(job.get("t"))
            if ss:
                eff = max(eff - ss, 0.1)
            if t:
                eff = min(eff, t)

        if job["kind"] == "mux":
            head = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-progress", "pipe:1", "-nostats"]
            if job["mode"] == "mix":
                cmds = [head + ["-i", video, "-i", audio, "-filter_complex",
                                "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[a]",
                                "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                "-movflags", "+faststart", out]]
            else:
                cmds = [head + ["-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                                "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out]]
            temps = []
        else:
            cmds, temps = build_commands(job, src, out, S, self.ffmpeg, eff)
            if job.get("luis"):
                self.log(f"Length {eff:.1f}s -> video bitrate {luis_bitrate(eff)}k" if eff else "Length unknown, using 1012k")

        try:
            for si, cmd in enumerate(cmds):
                if S["show_cmd"]:
                    self.log(subprocess.list2cmdline(cmd))
                self.run_cmd(cmd, eff, i, n, si, len(cmds))
        except Exception:
            if os.path.exists(out):
                try:
                    os.remove(out)
                except OSError:
                    pass
            raise
        finally:
            for t in temps:
                try:
                    os.remove(t)
                except OSError:
                    pass
        try:
            a, b = os.path.getsize(src) // 1024, os.path.getsize(out) // 1024
            self.log(f"OK: {a} KB -> {b} KB   saved: {out}")
        except OSError:
            self.log(f"OK: saved {out}")
        return out

    def run_cmd(self, cmd, dur, i, n, si, ns):
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        err = []
        t = threading.Thread(target=lambda: err.extend(self.proc.stderr.readlines()), daemon=True)
        t.start()
        for line in self.proc.stdout:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    secs = int(line.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                if dur:
                    frac = min(secs / dur, 1.0)
                    self.q.put(("prog", (i + (si + frac) / ns) / n))
        rc = self.proc.wait()
        t.join(timeout=2)
        if self.cancel:
            raise RuntimeError("cancelled")
        if rc != 0:
            msg = "".join(err).strip().splitlines()
            raise RuntimeError("ffmpeg error: " + (" | ".join(msg[-3:]) if msg else f"exit code {rc}"))


# ====================================================================
#  GUI building blocks
# ====================================================================
class Tab(ttk.Frame):
    scan_audio = False

    def __init__(self, app, nb):
        super().__init__(nb, padding=12)
        self.app = app

    def get_job(self):
        raise NotImplementedError


def labeled_entry(parent, row, col, text, var, width=10):
    ttk.Label(parent, text=text).grid(row=row, column=col, sticky="e", padx=(0, 6), pady=3)
    e = ttk.Entry(parent, textvariable=var, width=width)
    e.grid(row=row, column=col + 1, sticky="w", pady=3)
    return e


class Editor(ttk.Frame):
    """All the manual video/audio settings. Used by the Match/Custom tab."""

    def __init__(self, parent):
        super().__init__(parent)
        st = default_state()
        self.v = {}
        for k, val in st.items():
            self.v[k] = tk.IntVar(value=val) if isinstance(val, int) else tk.StringVar(value=val)
        v = self.v
        r = 0
        ttk.Checkbutton(self, text="Resolution", variable=v["use_res"]).grid(row=r, column=0, sticky="w")
        f = ttk.Frame(self); f.grid(row=r, column=1, sticky="w")
        ttk.Entry(f, textvariable=v["w"], width=6).pack(side="left")
        ttk.Label(f, text=" x ").pack(side="left")
        ttk.Entry(f, textvariable=v["h"], width=6).pack(side="left")
        ttk.Combobox(f, textvariable=v["resmode"], values=RES_MODES, state="readonly", width=20).pack(side="left", padx=8)
        r += 1
        for key, label, hint in (("fps", "Frame rate", "fps"), ("vb", "Video bitrate", "e.g. 500k, 2M, 800000"),
                                 ("ab", "Audio bitrate", "e.g. 32k, 128k")):
            ttk.Checkbutton(self, text=label, variable=v["use_" + {"fps": "fps", "vb": "vb", "ab": "ab"}[key]]).grid(row=r, column=0, sticky="w")
            f = ttk.Frame(self); f.grid(row=r, column=1, sticky="w")
            ttk.Entry(f, textvariable=v[key], width=12).pack(side="left")
            ttk.Label(f, text="  " + hint, foreground="gray").pack(side="left")
            r += 1
        ttk.Checkbutton(self, text="Audio sample rate / channels", variable=v["use_ar"]).grid(row=r, column=0, sticky="w")
        f = ttk.Frame(self); f.grid(row=r, column=1, sticky="w")
        ttk.Entry(f, textvariable=v["ar"], width=8).pack(side="left")
        ttk.Label(f, text=" Hz   ").pack(side="left")
        ttk.Entry(f, textvariable=v["ac"], width=4).pack(side="left")
        ttk.Label(f, text=" ch  (1 = mono)", foreground="gray").pack(side="left")
        r += 1
        ttk.Checkbutton(self, text="Mute audio", variable=v["mute"]).grid(row=r, column=0, sticky="w", pady=3)
        r += 1
        ttk.Label(self, text="Trim (optional)").grid(row=r, column=0, sticky="w")
        f = ttk.Frame(self); f.grid(row=r, column=1, sticky="w")
        ttk.Label(f, text="start ").pack(side="left")
        ttk.Entry(f, textvariable=v["ss"], width=9).pack(side="left")
        ttk.Label(f, text="  length ").pack(side="left")
        ttk.Entry(f, textvariable=v["t"], width=9).pack(side="left")
        ttk.Label(f, text="  seconds or hh:mm:ss", foreground="gray").pack(side="left")
        r += 1
        ttk.Label(self, text="Extra video filter").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=v["vf_extra"], width=55).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(self, text="Extra audio filter").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(self, textvariable=v["af_extra"], width=55).grid(row=r, column=1, sticky="w")
        r += 1
        ttk.Label(self, text="Filter examples: hue=s=0 (black and white)   hflip   negate   |   audio: volume=2   bass=g=10",
                  foreground="gray").grid(row=r, column=0, columnspan=2, sticky="w")

    def get_state(self):
        return {k: var.get() for k, var in self.v.items()}

    def set_state(self, st):
        base = default_state()
        base.update(st)
        for k, val in base.items():
            if k in self.v:
                self.v[k].set(val)


# ====================================================================
#  Tabs
# ====================================================================
class MatchCustomTab(Tab):
    def __init__(self, app, nb):
        super().__init__(app, nb)
        self.loaded_name = None
        self.ref_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="Custom")

        box = ttk.LabelFrame(self, text="Quality Match: copy the specs of a reference video", padding=8)
        box.pack(fill="x")
        ttk.Entry(box, textvariable=self.ref_var).pack(side="left", fill="x", expand=True)
        ttk.Button(box, text="Browse...", command=self.browse).pack(side="left", padx=4)
        ttk.Button(box, text="Read specs", command=self.read_specs).pack(side="left")
        self.spec_label = ttk.Label(self, text="Pick a reference video and press 'Read specs', or just edit the values below (Custom).",
                                    foreground="gray")
        self.spec_label.pack(anchor="w", pady=(4, 8))

        mf = ttk.Frame(self); mf.pack(anchor="w")
        ttk.Label(mf, text="Save exports to folder:").pack(side="left")
        for m in ("Quality Matched", "Custom"):
            ttk.Radiobutton(mf, text=m, value=m, variable=self.mode_var, command=self._mode_changed).pack(side="left", padx=6)

        self.editor = Editor(self)
        self.editor.pack(anchor="w", pady=10)
        bf = ttk.Frame(self); bf.pack(anchor="w")
        ttk.Button(bf, text="Save these settings as a preset...", command=self.save_preset).pack(side="left")
        ttk.Button(bf, text="Reset", command=self.reset).pack(side="left", padx=6)

    def _mode_changed(self):
        self.loaded_name = None

    def browse(self):
        p = filedialog.askopenfilename(title="Reference video")
        if p:
            self.ref_var.set(p)
            self.read_specs()

    def read_specs(self):
        p = self.ref_var.get().strip().strip('"')
        if not p or not os.path.exists(p):
            messagebox.showerror("Reference", "Pick a reference video first.")
            return
        if not find_tool("ffprobe"):
            self.app.need_ffmpeg()
            return
        try:
            sp = read_reference(p)
        except ValueError as e:
            messagebox.showerror("Reference", str(e))
            return
        st = self.editor.get_state()
        st.update({"w": sp["w"], "h": sp["h"], "fps": sp["fps"], "vb": sp["vb"], "ab": sp["ab"],
                   "ar": sp["ar"], "ac": sp["ac"], "resmode": RES_MODES[1]})
        self.editor.set_state(st)
        self.mode_var.set("Quality Matched")
        self.loaded_name = None
        self.spec_label.config(text=f"Matched: {sp['w']}x{sp['h']}, {sp['fps']} fps, video {sp['vb']} bps, "
                                    f"audio {sp['ab']} bps, {sp['ar']} Hz, {sp['ac']} ch")

    def reset(self):
        self.editor.set_state(default_state())
        self.loaded_name = None
        self.mode_var.set("Custom")

    def load_preset_state(self, name, state):
        self.editor.set_state(state)
        self.loaded_name = name
        self.spec_label.config(text=f"Loaded preset: {name}")

    def save_preset(self):
        name = simpledialog.askstring("Save preset", "Preset name:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name or re.search(r'[<>:"/\\|?*]', name):
            messagebox.showerror("Save preset", 'Names can\'t contain  \\ / : * ? " < > |')
            return
        os.makedirs(PRESET_DIR, exist_ok=True)
        path = os.path.join(PRESET_DIR, name + ".json")
        if os.path.exists(path) and not messagebox.askyesno("Save preset", "A preset with that name exists. Overwrite?"):
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.editor.get_state(), f, indent=2)
        self.app.my_tab.refresh()
        messagebox.showinfo("Save preset", f"Saved '{name}'. Find it in the My Presets tab.")

    def get_job(self):
        st = self.editor.get_state()
        if self.loaded_name:
            return state_to_job(st, safe_name(self.loaded_name), f"My Presets/{self.loaded_name}")
        if self.mode_var.get() == "Quality Matched":
            return state_to_job(st, "matched", "Quality Matched")
        return state_to_job(st, "custom", "Custom")


class PresetsTab(Tab):
    def __init__(self, app, nb):
        super().__init__(app, nb)
        ttk.Label(self, text="Pick a preset, add your files below, then press RUN.").pack(anchor="w")
        self.lb = tk.Listbox(self, height=len(BUILTINS), exportselection=False, font=("Segoe UI", 10))
        for name in BUILTINS:
            self.lb.insert("end", name)
        self.lb.selection_set(0)
        self.lb.pack(fill="x", pady=8)
        self.lb.bind("<<ListboxSelect>>", lambda e: self.update_desc())
        self.desc = ttk.Label(self, text="", wraplength=700, foreground="gray")
        self.desc.pack(anchor="w")

        self.luis_var = tk.IntVar(value=1)
        self.luis_chk = ttk.Checkbutton(
            self, text="luisgamercool23: lower the bitrate for shorter videos (scaled by frame count, never below 100k)",
            variable=self.luis_var)
        self.luis_chk.pack(anchor="w", pady=(10, 0))
        mf = ttk.Frame(self); mf.pack(anchor="w", pady=6)
        ttk.Label(mf, text="Corrupt amount (lower = more damage):").pack(side="left")
        self.amount = tk.StringVar(value="1500")
        ttk.Entry(mf, textvariable=self.amount, width=8).pack(side="left", padx=6)
        self.update_desc()

    def selected(self):
        return self.lb.get(self.lb.curselection()[0])

    def update_desc(self):
        self.desc.config(text=BUILTINS[self.selected()]["desc"])

    def get_job(self):
        name = self.selected()
        job = dict(BUILTINS[name]["job"])
        if job.get("warn") == "loud" and not messagebox.askokcancel(
                "Earrape", "This one is extremely loud and distorted.\nTurn your volume down before playing the result."):
            raise ValueError("Cancelled.")
        if name == "luisgamercool23 quality":
            job["luis"] = bool(self.luis_var.get())
        if name.startswith("Corrupt"):
            job["bsf"] = "amount=" + need(self.amount.get(), r"\d+", "Corrupt amount")
        return job


class MyPresetsTab(Tab):
    def __init__(self, app, nb):
        super().__init__(app, nb)
        ttk.Label(self, text="Presets you saved from the Match / Custom tab.").pack(anchor="w")
        self.lb = tk.Listbox(self, height=12, exportselection=False, font=("Segoe UI", 10))
        self.lb.pack(fill="x", pady=8)
        bf = ttk.Frame(self); bf.pack(anchor="w")
        ttk.Button(bf, text="Open in editor", command=self.to_editor).pack(side="left")
        ttk.Button(bf, text="Delete", command=self.delete).pack(side="left", padx=6)
        ttk.Button(bf, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Label(self, text="Select one and press RUN to use it as-is, or 'Open in editor' to tweak it first.",
                  foreground="gray").pack(anchor="w", pady=8)
        self.refresh()

    def names(self):
        try:
            return sorted(os.path.splitext(f)[0] for f in os.listdir(PRESET_DIR) if f.endswith(".json"))
        except OSError:
            return []

    def refresh(self):
        self.lb.delete(0, "end")
        for n in self.names():
            self.lb.insert("end", n)

    def current(self):
        sel = self.lb.curselection()
        if not sel:
            raise ValueError("Select a preset first.")
        name = self.lb.get(sel[0])
        with open(os.path.join(PRESET_DIR, name + ".json"), "r", encoding="utf-8") as f:
            return name, json.load(f)

    def to_editor(self):
        try:
            name, st = self.current()
        except ValueError as e:
            messagebox.showinfo("My Presets", str(e))
            return
        self.app.match_tab.load_preset_state(name, st)
        self.app.nb.select(self.app.match_tab)

    def delete(self):
        try:
            name, _ = self.current()
        except ValueError as e:
            messagebox.showinfo("My Presets", str(e))
            return
        if messagebox.askyesno("Delete preset", f"Delete '{name}'?"):
            os.remove(os.path.join(PRESET_DIR, name + ".json"))
            self.refresh()

    def get_job(self):
        name, st = self.current()
        base = default_state()
        base.update(st)
        return state_to_job(base, safe_name(name), f"My Presets/{name}")


class AudioFormat(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.fmt = tk.StringVar(value="MP3")
        self.br = tk.StringVar(value="192k")
        ttk.Label(self, text="Audio format").pack(side="left")
        cb = ttk.Combobox(self, textvariable=self.fmt, values=list(AUDIO_FORMATS), state="readonly", width=16)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.br.set(AUDIO_FORMATS[self.fmt.get()][2] or ""))
        ttk.Label(self, text="Bitrate").pack(side="left", padx=(8, 4))
        ttk.Entry(self, textvariable=self.br, width=8).pack(side="left")

    def targs(self, af=""):
        ext, codec, default_br = AUDIO_FORMATS[self.fmt.get()]
        args = ["-vn", "-map_metadata", "0"] + codec
        br = self.br.get().strip()
        if default_br and br:
            args += ["-b:a", need(br, RE_BR, "Audio bitrate")]
        if af:
            args += ["-af", af]
        return ext, args


class ConvertTab(Tab):
    FORMATS = ["MP4 (H.264)", "MKV", "WEBM (VP9, slow but small)", "MOV", "AVI", "GIF",
               "Fast remux to MP4 (no re-encode)", "Fast remux to MKV (no re-encode)", "Video to audio"]

    def __init__(self, app, nb):
        super().__init__(app, nb)
        self.fmt = tk.StringVar(value=self.FORMATS[0])
        ttk.Label(self, text="Convert to:").pack(anchor="w")
        for f in self.FORMATS:
            ttk.Radiobutton(self, text=f, value=f, variable=self.fmt, command=self.changed).pack(anchor="w")
        opt = ttk.LabelFrame(self, text="Options", padding=8)
        opt.pack(anchor="w", fill="x", pady=10)
        self.crf = tk.StringVar(value="20")
        labeled_entry(opt, 0, 0, "Quality CRF (lower = better, bigger)", self.crf, 6)
        self.gif_fps = tk.StringVar(value="15")
        self.gif_w = tk.StringVar(value="480")
        self.gif_ss = tk.StringVar()
        self.gif_t = tk.StringVar()
        labeled_entry(opt, 1, 0, "GIF frames per second", self.gif_fps, 6)
        labeled_entry(opt, 2, 0, "GIF width (pixels)", self.gif_w, 6)
        labeled_entry(opt, 3, 0, "GIF start (seconds, optional)", self.gif_ss, 6)
        labeled_entry(opt, 4, 0, "GIF length (seconds, optional)", self.gif_t, 6)
        self.afmt = AudioFormat(opt)
        self.afmt.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

    @property
    def scan_audio(self):
        return self.fmt.get() == "Video to audio"

    def changed(self):
        self.crf.set("32" if self.fmt.get().startswith("WEBM") else "20")

    def get_job(self):
        sel = self.fmt.get()
        S = self.app.S
        job = {"kind": "tool", "tag": "converted"}

        def x264():
            crf = need(self.crf.get(), RE_INT, "CRF")
            return ["-c:v", "libx264", "-preset", S["speed"], "-crf", crf, "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k"]

        if sel.startswith("MP4"):
            job.update(ext="mp4", folder="Converted/MP4", targs=x264() + ["-movflags", "+faststart"])
        elif sel == "MKV":
            job.update(ext="mkv", folder="Converted/MKV", targs=x264())
        elif sel.startswith("WEBM"):
            crf = need(self.crf.get(), RE_INT, "CRF")
            job.update(ext="webm", folder="Converted/WEBM",
                       targs=["-c:v", "libvpx-vp9", "-crf", crf, "-b:v", "0", "-row-mt", "1",
                              "-c:a", "libopus", "-b:a", "128k"])
        elif sel == "MOV":
            job.update(ext="mov", folder="Converted/MOV", targs=x264() + ["-movflags", "+faststart"])
        elif sel == "AVI":
            job.update(ext="avi", folder="Converted/AVI",
                       targs=["-c:v", "mpeg4", "-q:v", "3", "-c:a", "libmp3lame", "-b:a", "192k"])
        elif sel == "GIF":
            fps = need(self.gif_fps.get(), RE_NUM, "GIF fps")
            w = need(self.gif_w.get(), r"\d+", "GIF width")
            g = (f"fps={fps},scale={w}:-1:flags=lanczos,split[s0][s1];"
                 "[s0]palettegen[p];[s1][p]paletteuse")
            job.update(ext="gif", folder="Converted/GIF", targs=["-vf", g, "-an", "-loop", "0"])
            for key, var in (("ss", self.gif_ss), ("t", self.gif_t)):
                if var.get().strip():
                    if parse_time(var.get()) is None:
                        raise ValueError("GIF start/length doesn't look right.")
                    job[key] = var.get().strip()
        elif sel.startswith("Fast remux to MP4"):
            job.update(ext="mp4", folder="Converted/Remux MP4", targs=["-c", "copy", "-movflags", "+faststart"])
        elif sel.startswith("Fast remux to MKV"):
            job.update(ext="mkv", folder="Converted/Remux MKV", targs=["-c", "copy"])
        else:
            ext, targs = self.afmt.targs()
            job.update(ext=ext, folder="Converted/Audio", targs=targs)
        return job


class AudioToolsTab(Tab):
    # name: (param label, default, tag, folder)
    OPS = {
        "Convert / extract audio": (None, "", "converted", "Converted"),
        "Normalize loudness": (None, "", "normalized", "Normalized"),
        "Change volume (dB)": ("dB (6 = louder, -6 = quieter)", "6", "volume", "Volume"),
        "Bass boost (dB)": ("Gain in dB", "10", "bassboost", "Bass Boost"),
        "Sped up (faster + higher pitch)": (None, "", "spedup", "Sped Up"),
        "Slowed + reverb": (None, "", "slowed_reverb", "Slowed Reverb"),
        "Change speed (keeps pitch)": ("Multiplier, 0.5 to 2.0", "1.25", "speed", "Speed"),
        "Trim / cut only": (None, "", "trimmed", "Trimmed"),
        "Remove the audio from a video": (None, "", "muted", "Muted Videos"),
        "Add / replace audio in a video": (None, "", "newaudio", "Muxed"),
    }

    def __init__(self, app, nb):
        super().__init__(app, nb)
        ttk.Label(self, text="Works on audio files AND videos (a video gives you its audio).").pack(anchor="w")
        self.op = tk.StringVar(value=list(self.OPS)[0])
        cb = ttk.Combobox(self, textvariable=self.op, values=list(self.OPS), state="readonly", width=40)
        cb.pack(anchor="w", pady=8)
        cb.bind("<<ComboboxSelected>>", lambda e: self.changed())
        pf = ttk.Frame(self); pf.pack(anchor="w")
        self.param_lbl = ttk.Label(pf, text="")
        self.param_lbl.pack(side="left")
        self.param = tk.StringVar()
        self.param_entry = ttk.Entry(pf, textvariable=self.param, width=8)
        self.param_entry.pack(side="left", padx=6)
        tf = ttk.Frame(self); tf.pack(anchor="w", pady=6)
        ttk.Label(tf, text="Trim (optional): start").pack(side="left")
        self.ss = tk.StringVar(); self.t = tk.StringVar()
        ttk.Entry(tf, textvariable=self.ss, width=8).pack(side="left", padx=4)
        ttk.Label(tf, text="length").pack(side="left")
        ttk.Entry(tf, textvariable=self.t, width=8).pack(side="left", padx=4)
        self.afmt = AudioFormat(self)
        self.afmt.pack(anchor="w", pady=6)

        mf = ttk.LabelFrame(self, text="Add / replace audio (this one ignores the queue below)", padding=8)
        mf.pack(fill="x", pady=10)
        self.mv = tk.StringVar(); self.ma = tk.StringVar(); self.mmode = tk.StringVar(value="replace")
        for r, (lbl, var) in enumerate((("Video", self.mv), ("Audio file", self.ma))):
            ttk.Label(mf, text=lbl).grid(row=r, column=0, sticky="e", padx=4, pady=2)
            ttk.Entry(mf, textvariable=var, width=60).grid(row=r, column=1, pady=2)
            ttk.Button(mf, text="Browse...", command=lambda v=var: self.pick(v)).grid(row=r, column=2, padx=4)
        ttk.Radiobutton(mf, text="Replace the video's audio", value="replace", variable=self.mmode).grid(row=2, column=1, sticky="w")
        ttk.Radiobutton(mf, text="Mix on top of the original audio", value="mix", variable=self.mmode).grid(row=3, column=1, sticky="w")
        self.changed()

    def pick(self, var):
        p = filedialog.askopenfilename()
        if p:
            var.set(p)

    @property
    def scan_audio(self):
        return not self.op.get().startswith("Remove")

    def changed(self):
        label, default, _, _ = self.OPS[self.op.get()]
        self.param_lbl.config(text=label or "")
        self.param.set(default)
        self.param_entry.config(state="normal" if label else "disabled")

    def get_job(self):
        op = self.op.get()
        _, _, tag, folder = self.OPS[op]
        p = self.param.get().strip()
        job = {"kind": "tool", "tag": tag, "folder": f"Audio Tools/{folder}"}
        for key, var in (("ss", self.ss), ("t", self.t)):
            if var.get().strip():
                if parse_time(var.get()) is None:
                    raise ValueError("Trim time doesn't look right.")
                job[key] = var.get().strip()
        if op.startswith("Remove"):
            job.update(ext="same", targs=["-c:v", "copy", "-an"])
            return job
        if op.startswith("Add"):
            v, a = self.mv.get().strip().strip('"'), self.ma.get().strip().strip('"')
            if not (os.path.isfile(v) and os.path.isfile(a)):
                raise ValueError("Pick both a video and an audio file.")
            return {"kind": "mux", "tag": tag, "folder": f"Audio Tools/{folder}", "mode": self.mmode.get(),
                    "mux_files": [(v, a)]}
        af = ""
        if op.startswith("Normalize"):
            af = "loudnorm=I=-16:TP=-1.5:LRA=11"
        elif op.startswith("Change volume"):
            af = f"volume={need(p, RE_SNUM, 'dB')}dB"
        elif op.startswith("Bass"):
            af = f"bass=g={need(p, RE_SNUM, 'Gain')}"
        elif op.startswith("Sped"):
            af = "aresample=44100,asetrate=55125,aresample=44100"
        elif op.startswith("Slowed"):
            af = "aresample=44100,asetrate=37485,aresample=44100,aecho=0.8:0.88:60:0.4"
        elif op.startswith("Change speed"):
            af = f"atempo={need(p, RE_NUM, 'Speed')}"
        elif op.startswith("Trim") and not (job.get("ss") or job.get("t")):
            raise ValueError("Fill in a trim start and/or length.")
        ext, targs = self.afmt.targs(af)
        job.update(ext=ext, targs=targs)
        return job


class VideoDestroyerTab(Tab):
    def __init__(self, app, nb):
        super().__init__(app, nb)
        ttk.Label(self, text="Make videos look AND sound horrible.").pack(anchor="w")
        self.level = tk.StringVar(value="Bad")
        lf = ttk.Frame(self); lf.pack(anchor="w", pady=8)
        ttk.Label(lf, text="Level").pack(side="left")
        cb = ttk.Combobox(lf, textvariable=self.level, values=list(VD_LEVELS), state="readonly", width=18)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.load_level())
        box = ttk.LabelFrame(self, text="Values (you can edit any of them)", padding=8)
        box.pack(anchor="w", fill="x")
        self.f = {k: tk.StringVar() for k in ("h", "fps", "vb", "ab", "ar", "ac", "noise", "af")}
        for r, (k, lbl) in enumerate((("h", "Height (pixels)"), ("fps", "Frame rate"), ("vb", "Video bitrate"),
                                      ("ab", "Audio bitrate"), ("ar", "Audio sample rate (Hz)"),
                                      ("ac", "Audio channels"), ("noise", "Video noise 0-50"))):
            labeled_entry(box, r, 0, lbl, self.f[k], 10)
        ttk.Label(box, text="Audio filter").grid(row=7, column=0, sticky="e", padx=(0, 6))
        ttk.Entry(box, textvariable=self.f["af"], width=60).grid(row=7, column=1, sticky="w")
        self.stretch = tk.IntVar(value=1)
        ttk.Checkbutton(self, text="Stretch back up to 720p afterwards (plays big, looks blocky)",
                        variable=self.stretch).pack(anchor="w", pady=8)
        self.note = ttk.Label(self, text="", foreground="gray")
        self.note.pack(anchor="w")
        self.load_level()

    def load_level(self):
        for k, v in VD_LEVELS[self.level.get()].items():
            self.f[k].set(v)
        self.refresh_note()

    def refresh_note(self):
        self.note.config(text="Settings > 'Video Destroyer keeps original frame rate' is ON, so the fps value is ignored."
                         if self.app.S["vd_keepfps"] else "")

    def get_job(self):
        self.refresh_note()
        f = {k: v.get().strip() for k, v in self.f.items()}
        h = need(f["h"], r"\d+", "Height")
        vf = f"scale=-2:{h}"
        noise = need(f["noise"], r"\d+", "Noise")
        if noise != "0":
            vf += f",noise=alls={noise}:allf=t"
        if self.stretch.get():
            vf += ",scale=-2:720:flags=neighbor"
        job = {"kind": "enc", "tag": "destroyed", "folder": f"Destroyed/Video/{self.level.get()}", "ext": "mp4",
               "vf": vf, "vb": need(f["vb"], RE_BR, "Video bitrate"), "ab": need(f["ab"], RE_BR, "Audio bitrate"),
               "ar": need(f["ar"], r"\d+", "Sample rate"), "ac": need(f["ac"], r"\d+", "Channels"), "af": f["af"]}
        if not self.app.S["vd_keepfps"]:
            job["fps"] = need(f["fps"], RE_FPS, "Frame rate")
        return job


class AudioDestroyerTab(Tab):
    scan_audio = True

    def __init__(self, app, nb):
        super().__init__(app, nb)
        ttk.Label(self, text="Make audio sound horrible (saves as MP3). Works on audio files AND videos.").pack(anchor="w")
        self.level = tk.StringVar(value="Bad")
        lf = ttk.Frame(self); lf.pack(anchor="w", pady=8)
        ttk.Label(lf, text="Level").pack(side="left")
        cb = ttk.Combobox(lf, textvariable=self.level, values=list(AD_LEVELS), state="readonly", width=18)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.load_level())
        box = ttk.LabelFrame(self, text="Values (you can edit any of them)", padding=8)
        box.pack(anchor="w", fill="x")
        self.f = {k: tk.StringVar() for k in ("br", "ar", "ac", "af")}
        labeled_entry(box, 0, 0, "Bitrate (8k = worst)", self.f["br"], 10)
        labeled_entry(box, 1, 0, "Sample rate (Hz)", self.f["ar"], 10)
        labeled_entry(box, 2, 0, "Channels (1 = mono)", self.f["ac"], 10)
        ttk.Label(box, text="Audio filter").grid(row=3, column=0, sticky="e", padx=(0, 6))
        ttk.Entry(box, textvariable=self.f["af"], width=60).grid(row=3, column=1, sticky="w")
        ttk.Label(self, text="MP3 sample rates: 8000 11025 12000 16000 22050 24000 32000 44100 48000.  "
                             "'Max Destruction' is VERY loud.", foreground="gray").pack(anchor="w", pady=8)
        self.load_level()

    def load_level(self):
        for k, v in AD_LEVELS[self.level.get()].items():
            self.f[k].set(v)

    def get_job(self):
        f = {k: v.get().strip() for k, v in self.f.items()}
        if self.level.get() == "Max Destruction" and not messagebox.askokcancel(
                "Warning", "This is extremely loud and distorted.\nTurn your volume down before playing the result."):
            raise ValueError("Cancelled.")
        targs = ["-vn", "-map_metadata", "-1", "-c:a", "libmp3lame", "-b:a", need(f["br"], RE_BR, "Bitrate"),
                 "-ar", need(f["ar"], r"\d+", "Sample rate"), "-ac", need(f["ac"], r"\d+", "Channels")]
        if f["af"]:
            targs += ["-af", f["af"]]
        return {"kind": "tool", "tag": "destroyed", "folder": f"Destroyed/Audio/{self.level.get()}",
                "ext": "mp3", "targs": targs}


class SettingsTab(Tab):
    def __init__(self, app, nb):
        super().__init__(app, nb)
        S = app.S
        self.vars = {}

        def bind(key, var):
            self.vars[key] = var
            var.trace_add("write", lambda *a: self.changed(key))

        self.out = tk.StringVar(value=S["out_dir"]); bind("out_dir", self.out)
        ttk.Label(self, text="Output folder").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        ttk.Entry(self, textvariable=self.out, width=60).grid(row=0, column=1, pady=4)
        ttk.Button(self, text="Browse...", command=self.browse).grid(row=0, column=2, padx=6)
        ttk.Label(self, text="Encoder").grid(row=1, column=0, sticky="e", padx=6, pady=4)
        self.enc = tk.StringVar(value=S["encoder"]); bind("encoder", self.enc)
        ttk.Combobox(self, textvariable=self.enc, values=["libx264", "libx265", "h264_nvenc", "h264_amf", "h264_qsv"],
                     state="readonly", width=16).grid(row=1, column=1, sticky="w")
        ttk.Label(self, text="Encode speed").grid(row=2, column=0, sticky="e", padx=6, pady=4)
        self.speed = tk.StringVar(value=S["speed"]); bind("speed", self.speed)
        ttk.Combobox(self, textvariable=self.speed, values=["ultrafast", "veryfast", "medium", "slow"],
                     state="readonly", width=16).grid(row=2, column=1, sticky="w")
        for r, (key, text) in enumerate((
                ("overwrite", "Overwrite existing files (off = adds _1, _2... to the name)"),
                ("open_after", "Open the output folder when finished"),
                ("show_cmd", "Show the ffmpeg command in the log"),
                ("vd_keepfps", "Video Destroyer keeps the original frame rate")), start=3):
            v = tk.BooleanVar(value=S[key]); bind(key, v)
            ttk.Checkbutton(self, text=text, variable=v).grid(row=r, column=1, sticky="w", pady=2)
        ttk.Button(self, text="Open output folder", command=lambda: open_path(self.app.S["out_dir"])).grid(
            row=8, column=1, sticky="w", pady=10)
        ttk.Label(self, text=f"Settings and presets are stored in: {CFG_DIR}", foreground="gray").grid(
            row=9, column=0, columnspan=3, sticky="w", padx=6)

    def browse(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.out.set(d)

    def changed(self, key):
        val = self.vars[key].get()
        self.app.S[key] = val
        save_settings(self.app.S)
        if key == "vd_keepfps":
            self.app.vd_tab.refresh_note()

    def get_job(self):
        raise ValueError("Settings has nothing to run. Pick another tab.")


# ====================================================================
#  Main window
# ====================================================================
class App:
    def __init__(self, root):
        self.root = root
        self.S = load_settings()
        self.files = []
        self.runner = None
        self.q = queue.Queue()
        root.title("Video Toolkit")
        root.geometry("1000x860")
        root.minsize(900, 760)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=False, padx=8, pady=8)
        self.match_tab = MatchCustomTab(self, self.nb)
        self.presets_tab = PresetsTab(self, self.nb)
        self.my_tab = MyPresetsTab(self, self.nb)
        self.conv_tab = ConvertTab(self, self.nb)
        self.audio_tab = AudioToolsTab(self, self.nb)
        self.vd_tab = VideoDestroyerTab(self, self.nb)
        self.ad_tab = AudioDestroyerTab(self, self.nb)
        self.set_tab = SettingsTab(self, self.nb)
        for tab, name in ((self.match_tab, "Match / Custom"), (self.presets_tab, "Presets"), (self.my_tab, "My Presets"),
                          (self.conv_tab, "Video Converters"), (self.audio_tab, "Audio Tools"),
                          (self.vd_tab, "Video Destroyer"), (self.ad_tab, "Audio Destroyer"), (self.set_tab, "Settings")):
            self.nb.add(tab, text=name)

        # ---- queue ----
        qf = ttk.LabelFrame(root, text="Files to process (batch)" + ("  -  drag and drop works" if HAS_DND else ""), padding=6)
        qf.pack(fill="both", expand=True, padx=8)
        self.lb = tk.Listbox(qf, height=5, selectmode="extended")
        sb = ttk.Scrollbar(qf, command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        bf = ttk.Frame(qf); bf.pack(side="left", padx=6)
        for text, cmd in (("Add files...", self.add_files), ("Add folder...", self.add_folder),
                          ("Remove selected", self.remove_sel), ("Clear", self.clear)):
            ttk.Button(bf, text=text, command=cmd, width=16).pack(pady=2)
        if HAS_DND:
            for w in (self.lb, qf):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)

        # ---- run bar ----
        rf = ttk.Frame(root); rf.pack(fill="x", padx=8, pady=6)
        ttk.Label(rf, text="Output name (only used for a single file, optional):").pack(side="left")
        self.outname = tk.StringVar()
        ttk.Entry(rf, textvariable=self.outname, width=28).pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(rf, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        self.run_btn = ttk.Button(rf, text="RUN", command=self.run, width=14)
        self.run_btn.pack(side="right", padx=6)
        ttk.Button(rf, text="Open output folder", command=lambda: open_path(self.S["out_dir"])).pack(side="right")

        self.status = ttk.Label(root, text="Ready.")
        self.status.pack(anchor="w", padx=10)
        self.bar = ttk.Progressbar(root, maximum=1000)
        self.bar.pack(fill="x", padx=8, pady=4)
        self.log_box = tk.Text(root, height=9, state="disabled", wrap="word", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.vd_tab.refresh_note())
        root.after(100, self.poll)
        if not find_tool("ffmpeg") or not find_tool("ffprobe"):
            self.log("ffmpeg / ffprobe not found. Install with:  winget install Gyan.FFmpeg  (then restart this app), "
                     "or put ffmpeg.exe and ffprobe.exe next to this program.")
        for p in sys.argv[1:]:
            self.add_path(p)

    # ---- helpers ----
    def need_ffmpeg(self):
        messagebox.showerror("ffmpeg missing",
                             "ffmpeg / ffprobe were not found.\n\nInstall with:\n    winget install Gyan.FFmpeg\n"
                             "then restart this app, or put ffmpeg.exe and ffprobe.exe next to this program.")

    def log(self, text):
        self.log_box.config(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def current_tab(self):
        return self.nb.nametowidget(self.nb.select())

    # ---- queue ----
    def add_path(self, p):
        p = p.strip().strip('"')
        if os.path.isdir(p):
            scan_audio = getattr(self.current_tab(), "scan_audio", False)
            exts = VIDEO_EXT | (AUDIO_EXT if scan_audio else set())
            for name in sorted(os.listdir(p)):
                fp = os.path.join(p, name)
                if os.path.isfile(fp) and os.path.splitext(name)[1].lower() in exts:
                    self.add_file(fp)
        elif os.path.isfile(p):
            self.add_file(p)

    def add_file(self, fp):
        fp = os.path.normpath(fp)
        if fp not in self.files:
            self.files.append(fp)
            self.lb.insert("end", fp)

    def add_files(self):
        for p in filedialog.askopenfilenames(title="Add videos / audio"):
            self.add_file(p)

    def add_folder(self):
        d = filedialog.askdirectory(title="Add a folder")
        if d:
            self.add_path(d)

    def on_drop(self, event):
        for p in self.root.tk.splitlist(event.data):
            self.add_path(p)

    def remove_sel(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
            del self.files[i]

    def clear(self):
        self.lb.delete(0, "end")
        self.files.clear()

    # ---- running ----
    def run(self):
        tab = self.current_tab()
        if not (find_tool("ffmpeg") and find_tool("ffprobe")):
            self.need_ffmpeg()
            return
        try:
            job = tab.get_job()
        except ValueError as e:
            messagebox.showerror("Can't run", str(e))
            return
        except Exception as e:
            messagebox.showerror("Can't run", f"{type(e).__name__}: {e}")
            return
        files = job["mux_files"] if job["kind"] == "mux" else list(self.files)
        if not files:
            messagebox.showinfo("No files", "Add some files to the queue first.")
            return
        outname = safe_name(self.outname.get()) or None
        self.S["out_dir"] = self.S.get("out_dir") or CFG_DIR
        os.makedirs(self.S["out_dir"], exist_ok=True)
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.bar["value"] = 0
        self.runner = Runner(files, job, dict(self.S), self.q, find_tool("ffmpeg"), outname)
        self.runner.start()

    def cancel(self):
        if self.runner:
            self.runner.stop()
            self.status.config(text="Cancelling...")

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    self.log(msg[1])
                elif msg[0] == "file":
                    self.status.config(text=msg[1])
                elif msg[0] == "prog":
                    self.bar["value"] = msg[1] * 1000
                elif msg[0] == "done":
                    _, ok, fail, last, cancelled = msg
                    self.run_btn.config(state="normal")
                    self.cancel_btn.config(state="disabled")
                    self.bar["value"] = 0 if cancelled else 1000
                    text = "Cancelled." if cancelled else f"Finished: {ok} done, {fail} failed."
                    self.status.config(text=text)
                    self.log("=== " + text)
                    if not cancelled and last and self.S["open_after"]:
                        open_path(last, select=True)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform.startswith("win") else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
