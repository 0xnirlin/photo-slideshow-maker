#!/usr/bin/env python3
"""slideshow: turn a folder of photos into an MP4 slideshow with ffmpeg.

Each photo becomes its own short clip (Ken Burns pan and zoom with zoompan).
The clips are then joined in small batches with xfade crossfades, so memory
stays low however many photos you have. Standard library only.
"""

import argparse
import datetime
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import textwrap
import unicodedata

__version__ = "1.0.0"

SIZES = {
    "1080p": (1920, 1080),
    "720p": (1280, 720),
    "vertical": (1080, 1920),
    "square": (1080, 1080),
}
MOTIONS = ("zoom-in", "zoom-out", "pan", "none", "random")
TRANSITIONS = ("fade", "slideleft", "circleopen", "none")
FITS = ("blur", "contain", "crop")
SORTS = ("auto", "name", "date")
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
TEST_DIVISOR = 12      # hidden --test-size: output is 1/12 of the real size
OVERSAMPLE = 2         # zoompan works on a frame this many times bigger than the output
INTERMEDIATE_CRF = 14  # clips that will be re-encoded again
THREADS = 2            # x264 threads per ffmpeg call: fewer threads, less memory


class SlideshowError(Exception):
    """A problem the user can act on. Printed without a traceback."""


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def natural_key(text):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text.lower())]


def to_frames(seconds, fps):
    return max(1, int(round(seconds * fps)))


def parse_color(text):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", text.strip())
    if not m:
        raise argparse.ArgumentTypeError("bad color %r, use something like #111111" % text)
    h = m.group(1)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def output_size(size_name, test_size=False):
    w, h = SIZES[size_name]
    if test_size:
        w = max(2, int(round(w / TEST_DIVISOR / 2.0)) * 2)
        h = max(2, int(round(h / TEST_DIVISOR / 2.0)) * 2)
    return w, h


def expected_frames(slide_frames, overlap_frames):
    """Frames in the finished video: all slides, minus one overlap per join."""
    return sum(slide_frames) - (len(slide_frames) - 1) * overlap_frames


def explain(args, photo_count, slide_count, overlap):
    """The duration formula in words, with this run's numbers."""
    text = "%d photo(s) x %g s" % (photo_count, args.seconds)
    if args.title is not None:
        text += " + %g s title" % args.title_seconds
    if slide_count > 1:
        text += ", minus %d overlap(s) of %.2f s" % (slide_count - 1, overlap / float(args.fps))
    return text


def human_size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024.0 / 1024.0)
    return "%.1f KB" % (n / 1024.0)


# ----------------------------------------------------------------------------
# Finding and sorting photos
# ----------------------------------------------------------------------------

def read_exif_datetime(path):
    """DateTimeOriginal from a JPEG's EXIF block as 'YYYY:MM:DD HH:MM:SS'.

    Standard library only: walks the JPEG markers to APP1, then the TIFF
    directories. Returns None when the file has no usable date.
    """
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"\xff\xd8":
                return None
            while True:
                b = f.read(1)
                if not b:
                    return None
                if b != b"\xff":
                    continue
                marker = f.read(1)
                while marker == b"\xff":
                    marker = f.read(1)
                if not marker or marker[0] in (0xD9, 0xDA):  # EOI, start of scan
                    return None
                if marker[0] == 0x01 or 0xD0 <= marker[0] <= 0xD8:
                    continue
                length = struct.unpack(">H", f.read(2))[0]
                data = f.read(length - 2)
                if marker[0] == 0xE1 and data[:6] == b"Exif\x00\x00":
                    return _exif_date_from_tiff(data[6:])
    except (OSError, struct.error):
        return None


def _exif_date_from_tiff(tiff):
    if tiff[:2] == b"II":
        e = "<"
    elif tiff[:2] == b"MM":
        e = ">"
    else:
        return None

    def ifd_entries(offset):
        n = struct.unpack(e + "H", tiff[offset:offset + 2])[0]
        for i in range(n):
            ent = tiff[offset + 2 + 12 * i: offset + 14 + 12 * i]
            tag, typ, count = struct.unpack(e + "HHI", ent[:8])
            yield tag, typ, count, ent[8:12]

    def ascii_value(count, raw):
        if count <= 4:
            return raw[:count]
        off = struct.unpack(e + "I", raw)[0]
        return tiff[off:off + count]

    try:
        ifd0 = struct.unpack(e + "I", tiff[4:8])[0]
        exif_ifd = None
        fallback = None
        for tag, typ, count, raw in ifd_entries(ifd0):
            if tag == 0x8769:
                exif_ifd = struct.unpack(e + "I", raw)[0]
            elif tag == 0x0132 and typ == 2:
                fallback = ascii_value(count, raw)
        found = None
        if exif_ifd:
            for tag, typ, count, raw in ifd_entries(exif_ifd):
                if tag == 0x9003 and typ == 2:       # DateTimeOriginal
                    found = ascii_value(count, raw)
                    break
                if tag == 0x9004 and typ == 2 and found is None:  # DateTimeDigitized
                    found = ascii_value(count, raw)
        value = found or fallback
        if not value:
            return None
        text = value.split(b"\x00")[0].decode("ascii", "replace").strip()
        return text if re.fullmatch(r"\d{4}:\d{2}:\d{2} \d{2}:\d{2}:\d{2}", text) else None
    except (struct.error, IndexError):
        return None


def date_key(path):
    """EXIF date when there is one, else the file's modified time."""
    text = read_exif_datetime(path)
    if text:
        try:
            dt = datetime.datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
            return (dt.timestamp(), natural_key(os.path.basename(path)))
        except (ValueError, OverflowError, OSError):
            pass
    try:
        return (os.path.getmtime(path), natural_key(os.path.basename(path)))
    except OSError:
        return (0.0, natural_key(os.path.basename(path)))


def collect_photos(inputs, sort="auto"):
    """Expand folders and files into an ordered list of photo paths.

    auto: a folder's photos are sorted by name, files you list keep your order.
    name / date: sort everything together.
    """
    found = []
    for item in inputs:
        if os.path.isdir(item):
            names = [n for n in os.listdir(item)
                     if not n.startswith(".") and n.lower().endswith(PHOTO_EXTS)
                     and os.path.isfile(os.path.join(item, n))]
            names.sort(key=natural_key)
            found.extend(os.path.join(item, n) for n in names)
        elif os.path.isfile(item):
            if not item.lower().endswith(PHOTO_EXTS):
                raise SlideshowError("%s is not a supported photo (%s)"
                                     % (item, ", ".join(PHOTO_EXTS)))
            found.append(item)
        else:
            raise SlideshowError("no such file or folder: %s" % item)
    if not found:
        raise SlideshowError("no photos found (looked for %s)" % ", ".join(PHOTO_EXTS))
    if sort == "name":
        found.sort(key=lambda p: natural_key(os.path.basename(p)))
    elif sort == "date":
        found.sort(key=date_key)
    return found


# ----------------------------------------------------------------------------
# Title card: a tiny built-in pixel font, so no drawtext/freetype is needed
# ----------------------------------------------------------------------------

_FONT_SRC = """
A 01110 10001 10001 11111 10001 10001 10001
B 11110 10001 10001 11110 10001 10001 11110
C 01110 10001 10000 10000 10000 10001 01110
D 11110 10001 10001 10001 10001 10001 11110
E 11111 10000 10000 11110 10000 10000 11111
F 11111 10000 10000 11110 10000 10000 10000
G 01110 10001 10000 10111 10001 10001 01111
H 10001 10001 10001 11111 10001 10001 10001
I 01110 00100 00100 00100 00100 00100 01110
J 00111 00010 00010 00010 00010 10010 01100
K 10001 10010 10100 11000 10100 10010 10001
L 10000 10000 10000 10000 10000 10000 11111
M 10001 11011 10101 10101 10001 10001 10001
N 10001 11001 10101 10011 10001 10001 10001
O 01110 10001 10001 10001 10001 10001 01110
P 11110 10001 10001 11110 10000 10000 10000
Q 01110 10001 10001 10001 10101 10010 01101
R 11110 10001 10001 11110 10100 10010 10001
S 01111 10000 10000 01110 00001 00001 11110
T 11111 00100 00100 00100 00100 00100 00100
U 10001 10001 10001 10001 10001 10001 01110
V 10001 10001 10001 10001 10001 01010 00100
W 10001 10001 10001 10101 10101 10101 01010
X 10001 10001 01010 00100 01010 10001 10001
Y 10001 10001 01010 00100 00100 00100 00100
Z 11111 00001 00010 00100 01000 10000 11111
0 01110 10001 10011 10101 11001 10001 01110
1 00100 01100 00100 00100 00100 00100 01110
2 01110 10001 00001 00010 00100 01000 11111
3 11110 00001 00001 01110 00001 00001 11110
4 00010 00110 01010 10010 11111 00010 00010
5 11111 10000 11110 00001 00001 10001 01110
6 00110 01000 10000 11110 10001 10001 01110
7 11111 00001 00010 00100 01000 01000 01000
8 01110 10001 10001 01110 10001 10001 01110
9 01110 10001 10001 01111 00001 00010 01100
. 00000 00000 00000 00000 00000 01100 01100
, 00000 00000 00000 00000 01100 00100 01000
! 00100 00100 00100 00100 00100 00000 00100
? 01110 10001 00001 00010 00100 00000 00100
- 00000 00000 00000 11111 00000 00000 00000
' 00100 00100 01000 00000 00000 00000 00000
: 00000 01100 01100 00000 01100 01100 00000
& 01100 10010 10100 01000 10101 10010 01101
/ 00001 00010 00010 00100 01000 01000 10000
""".strip().splitlines()
FONT = {}
for _line in _FONT_SRC:
    _parts = _line.split()
    FONT[_parts[0]] = _parts[1:]
FONT[" "] = ["00000"] * 7


def _clean_title(text):
    out = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(ch):
            continue
        ch = ch.upper()
        if ch in ("\u2019", "\u2018"):
            ch = "'"
        out.append(ch if ch in FONT else ("?" if not ch.isspace() else " "))
    return re.sub(r"\s+", " ", "".join(out)).strip()


def title_layout(text, width, height):
    """Pick a dot size and wrap the text so it fits the frame."""
    text = _clean_title(text) or "?"
    scale = max(1, int(height * 0.09 / 7))
    longest = max(len(w) for w in text.split())
    while True:
        chars = max(1, int(width * 0.86) // (6 * scale))
        lines = textwrap.wrap(text, chars) or [text]
        fits = len(lines) * 9 * scale <= height * 0.8 and chars >= longest
        if scale == 1 or fits:
            break
        scale -= 1
    max_lines = max(1, int(height * 0.9) // (9 * scale))
    return lines[:max_lines], scale


def write_title_ppm(path, text, width, height, bg, fg):
    """Write the title card as a binary PPM image of exactly width x height."""
    lines, s = title_layout(text, width, height)
    block_h = len(lines) * 9 * s - 2 * s
    top = max(0, (height - block_h) // 2)
    bg_row = bytes(bg) * width
    rows = [bg_row] * height
    fg_px = bytes(fg)
    bg_px = bytes(bg)
    for li, line in enumerate(lines):
        line_w = len(line) * 6 * s - s
        left = max(0, (width - line_w) // 2)
        for gy in range(7):
            cells = bytearray()
            for ch in line:
                glyph = FONT.get(ch, FONT["?"])[gy]
                for bit in glyph:
                    cells += (fg_px if bit == "1" else bg_px) * s
                cells += bg_px * s
            row = bytearray(bg_row)
            seg = bytes(cells[:line_w * 3])
            seg = seg[:max(0, (width - left) * 3)]
            row[left * 3:left * 3 + len(seg)] = seg
            row = bytes(row)
            y0 = top + li * 9 * s + gy * s
            for k in range(s):
                if 0 <= y0 + k < height:
                    rows[y0 + k] = row
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (width, height))
        for r in rows:
            f.write(r)


# ----------------------------------------------------------------------------
# ffmpeg
# ----------------------------------------------------------------------------

def find_tools():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SlideshowError(
            "ffmpeg and ffprobe are needed and were not found on your PATH. "
            "Install ffmpeg (brew install ffmpeg, sudo apt install ffmpeg, "
            "winget install ffmpeg) and open a new terminal.")
    return ffmpeg, ffprobe


def run_ffmpeg(ffmpeg, args, what):
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"] + args
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-6:]
        raise SlideshowError("ffmpeg failed while %s:\n  %s" % (what, "\n  ".join(tail)))


def probe(ffprobe, path):
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,size:stream=codec_type,codec_name,width,height,nb_frames",
         "-of", "json", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise SlideshowError("ffprobe could not read %s" % path)
    info = json.loads(proc.stdout.decode("utf-8", "replace"))
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    fmt = info.get("format", {})
    return {
        "duration": float(fmt.get("duration", 0) or 0),
        "size": int(fmt.get("size", 0) or 0),
        "width": video[0].get("width") if video else None,
        "height": video[0].get("height") if video else None,
        "has_audio": bool(audio),
        "audio_codec": audio[0].get("codec_name") if audio else None,
    }


def pick_motions(motion, count, seed):
    """One motion per photo. random avoids repeating the same move twice in a row."""
    if motion != "random":
        return [motion] * count
    rng = random.Random(seed)
    pool = ["zoom-in", "zoom-out", "pan"]
    out = []
    for _ in range(count):
        choices = [m for m in pool if not out or m != out[-1]]
        out.append(rng.choice(choices))
    return out


def photo_filter(width, height, fit, motion, frames, fps, index, strength):
    """Filter graph for one photo: fit it to the frame, then zoompan."""
    o = OVERSAMPLE
    cw, ch = width * o, height * o
    if fit == "crop":
        base = "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d" % (cw, ch, cw, ch)
    elif fit == "contain":
        base = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
                "pad=%d:%d:(ow-iw)/2:(oh-ih)/2:black" % (cw, ch, cw, ch))
    else:  # blur: sharp photo centred over a blurred, enlarged copy of itself
        bw, bh = max(2, cw // 8), max(2, ch // 8)
        radius = max(1, min(bw, bh) // 14)
        base = ("split=2[bg][fg];"
                "[bg]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                "scale=%d:%d,boxblur=%d:2,scale=%d:%d[b];"
                "[fg]scale=%d:%d:force_original_aspect_ratio=decrease[f];"
                "[b][f]overlay=(W-w)/2:(H-h)/2" % (cw, ch, cw, ch, bw, bh, radius, cw, ch, cw, ch))
    d = max(frames - 1, 1)
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    s = "%.4f" % strength
    if motion == "zoom-in":
        z, x, y = "1+%s*on/%d" % (s, d), cx, cy
    elif motion == "zoom-out":
        z, x, y = "1+%s-%s*on/%d" % (s, s, d), cx, cy
    elif motion == "pan":
        z = "1+%s" % s
        x = ("(iw-iw/zoom)*on/%d" if index % 2 == 0 else "(iw-iw/zoom)*(1-on/%d)") % d
        y = cy
    else:
        z, x, y = "1", cx, cy
    zp = "zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d" % (z, x, y, frames, width, height, fps)
    return "%s,format=yuv420p,setsar=1,%s,format=yuv420p,setsar=1" % (base, zp)


def encode_args(crf, preset, fps):
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-r", str(fps), "-video_track_timescale", "90000", "-an"] + (
        ["-threads", str(THREADS)] if THREADS else [])


def render_photo_clip(ffmpeg, photo, out, args, width, height, frames, motion, index):
    vf = photo_filter(width, height, args.fit, motion, frames, args.fps, index, args.zoom_strength)
    run_ffmpeg(ffmpeg, ["-i", photo, "-vf", vf, "-frames:v", str(frames)]
               + encode_args(INTERMEDIATE_CRF, "veryfast", args.fps) + [out],
               "rendering %s" % os.path.basename(photo))


def render_title_clip(ffmpeg, tmp, args, width, height, frames):
    ppm = os.path.join(tmp, "title.ppm")
    write_title_ppm(ppm, args.title, width, height, args.title_bg, args.title_color)
    out = os.path.join(tmp, "clip_title.mp4")
    run_ffmpeg(ffmpeg, ["-loop", "1", "-framerate", str(args.fps), "-i", ppm,
                        "-frames:v", str(frames), "-vf", "format=yuv420p,setsar=1"]
               + encode_args(INTERMEDIATE_CRF, "veryfast", args.fps) + [out],
               "rendering the title card")
    return out


def join_clips(ffmpeg, clips, overlap, transition, fps, out, crf, preset, final):
    """Join (path, frames) clips with xfade (or a hard cut). Returns frames."""
    n = len(clips)
    parts = ["[%d:v]setpts=PTS-STARTPTS,fps=%d,format=yuv420p,setsar=1[v%d]" % (i, fps, i)
             for i in range(n)]
    total = clips[0][1]
    if n == 1:
        parts.append("[v0]null[vout]")
    elif transition == "none" or overlap == 0:
        parts.append("%sconcat=n=%d:v=1:a=0[vout]" % ("".join("[v%d]" % i for i in range(n)), n))
        total = sum(f for _, f in clips)
    else:
        prev = "v0"
        for k in range(1, n):
            offset = total - overlap
            label = "vout" if k == n - 1 else "x%d" % k
            parts.append("[%s][v%d]xfade=transition=%s:duration=%.6f:offset=%.6f[%s]"
                         % (prev, k, transition, overlap / float(fps), offset / float(fps), label))
            prev = label
            total = total + clips[k][1] - overlap
    cmd = []
    for path, _ in clips:
        cmd += ["-i", path]
    cmd += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
    cmd += encode_args(crf, preset, fps)
    if final:
        cmd += ["-movflags", "+faststart"]
    run_ffmpeg(ffmpeg, cmd + [out], "joining clips")
    return total


def join_in_batches(ffmpeg, clips, tmp, batch, overlap, transition, fps, crf, preset, log):
    """Join any number of clips by joining small batches, then joining the results."""
    level = 0
    while True:
        groups = [clips[i:i + batch] for i in range(0, len(clips), batch)]
        final = len(groups) == 1
        new = []
        for gi, group in enumerate(groups):
            if len(group) == 1 and not final:
                new.append(group[0])
                continue
            out = os.path.join(tmp, "video.mp4" if final else "join_%d_%d.mp4" % (level, gi))
            log("joining %d clip(s)%s" % (len(group), "" if final else
                                         " (batch %d of %d, level %d)" % (gi + 1, len(groups), level + 1)))
            frames = join_clips(ffmpeg, group, overlap, transition, fps, out,
                                crf if final else INTERMEDIATE_CRF,
                                preset if final else "veryfast", final)
            new.append((out, frames))
        clips = new
        if final:
            return clips[0]
        level += 1


def add_music(ffmpeg, video, music, out, duration, fade):
    cmd = ["-i", video, "-stream_loop", "-1", "-i", music, "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    fade = min(fade, duration / 2.0)
    if fade > 0:
        cmd += ["-af", "afade=t=out:st=%.3f:d=%.3f" % (duration - fade, fade)]
    cmd += ["-t", "%.6f" % duration, "-movflags", "+faststart", out]
    run_ffmpeg(ffmpeg, cmd, "adding the music")


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="slideshow",
        description="Turn a folder of photos into an MP4 slideshow with Ken Burns motion, "
                    "crossfades, an optional title card and optional music.")
    p.add_argument("inputs", nargs="+", metavar="PHOTO_OR_FOLDER",
                   help="photos and/or folders of photos (JPEG, PNG, WebP, BMP, TIFF)")
    p.add_argument("-o", "--output", default="slideshow.mp4", help="output file (default slideshow.mp4)")
    p.add_argument("--overwrite", action="store_true", help="replace the output file if it exists")
    p.add_argument("--seconds", type=float, default=3.0,
                   help="seconds each photo is on screen, overlaps included (default 3)")
    p.add_argument("--motion", choices=MOTIONS, default="random",
                   help="Ken Burns motion (default random)")
    p.add_argument("--zoom-strength", type=float, default=0.15,
                   help="how far the zoom goes, 0.15 = 15 percent (default 0.15)")
    p.add_argument("--transition", choices=TRANSITIONS, default="fade", help="default fade")
    p.add_argument("--transition-seconds", type=float, default=0.8,
                   help="overlap between photos (default 0.8, at most half of --seconds)")
    p.add_argument("--size", choices=sorted(SIZES), default="1080p",
                   help="1080p 1920x1080, 720p 1280x720, vertical 1080x1920, square 1080x1080")
    p.add_argument("--fit", choices=FITS, default="blur",
                   help="blur: photo over a blurred copy, contain: black bars, crop: fill the frame")
    p.add_argument("--fps", type=int, default=30, help="frames per second (default 30)")
    p.add_argument("--sort", choices=SORTS, default="auto",
                   help="auto: folders by name, listed files in your order. name or date sorts all")
    p.add_argument("--title", help="text for a title card shown first (built-in pixel font)")
    p.add_argument("--title-seconds", type=float, default=3.0, help="title card length (default 3)")
    p.add_argument("--title-bg", type=parse_color, default=parse_color("#111111"),
                   help="title background color (default #111111)")
    p.add_argument("--title-color", type=parse_color, default=parse_color("#ffffff"),
                   help="title text color (default #ffffff)")
    p.add_argument("--music", metavar="FILE", help="audio file to play under the slideshow")
    p.add_argument("--music-fade", type=float, default=2.0,
                   help="fade-out length at the end in seconds, 0 for none (default 2)")
    p.add_argument("--batch", type=int, default=None,
                   help="clips joined per ffmpeg call, lower uses less memory "
                        "(default 6 up to 720p, 2 for bigger sizes)")
    p.add_argument("--crf", type=int, default=18, help="x264 quality, lower is better (default 18)")
    p.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    p.add_argument("--threads", type=int, default=THREADS,
                   help="x264 threads per ffmpeg call, 0 = ffmpeg decides (default %d)" % THREADS)
    p.add_argument("--seed", type=int, default=1, help="seed for --motion random (default 1)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and the length, render nothing")
    p.add_argument("--test-size", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--version", action="version", version="slideshow " + __version__)
    return p


def validate(args, parser):
    if args.seconds <= 0:
        parser.error("--seconds must be above 0")
    if args.fps < 1 or args.fps > 120:
        parser.error("--fps must be between 1 and 120")
    if args.batch is not None and args.batch < 2:
        parser.error("--batch must be at least 2")
    if args.zoom_strength < 0 or args.zoom_strength > 1:
        parser.error("--zoom-strength must be between 0 and 1")
    if args.music_fade < 0:
        parser.error("--music-fade cannot be negative")
    if args.title is not None and args.title_seconds <= 0:
        parser.error("--title-seconds must be above 0")
    slide = to_frames(args.seconds, args.fps)
    overlap = 0 if args.transition == "none" else to_frames(args.transition_seconds, args.fps)
    if args.transition != "none":
        if args.transition_seconds <= 0:
            parser.error("--transition-seconds must be above 0 (or use --transition none)")
        if 2 * overlap > slide:
            parser.error("--transition-seconds %.2f is too long for --seconds %.2f: "
                         "the overlap can be at most half of a photo's time"
                         % (args.transition_seconds, args.seconds))
        if args.title is not None and 2 * overlap > to_frames(args.title_seconds, args.fps):
            parser.error("--transition-seconds is too long for --title-seconds")
    return slide, overlap


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    slide_frames_each, overlap = validate(args, parser)
    global THREADS
    THREADS = max(0, args.threads)
    log = lambda msg: print(msg, file=sys.stderr)
    try:
        ffmpeg, ffprobe = find_tools()
        photos = collect_photos(args.inputs, args.sort)
        if args.music and not os.path.isfile(args.music):
            raise SlideshowError("no such music file: %s" % args.music)
        if os.path.exists(args.output) and not args.overwrite and not args.dry_run:
            raise SlideshowError("%s already exists, use --overwrite to replace it" % args.output)

        width, height = output_size(args.size, args.test_size)
        if args.batch is None:
            args.batch = 6 if width * height <= 1000000 else 2
        slides = []  # frames per slide, title first
        if args.title is not None:
            slides.append(to_frames(args.title_seconds, args.fps))
        slides += [slide_frames_each] * len(photos)
        total_frames = expected_frames(slides, overlap)
        total_seconds = total_frames / float(args.fps)
        motions = pick_motions(args.motion, len(photos), args.seed)

        if args.dry_run:
            print("%d photo(s), %dx%d, %d fps" % (len(photos), width, height, args.fps))
            for i, ph in enumerate(photos):
                print("  %3d  %-9s %s" % (i + 1, motions[i], ph))
            print("length: %.2f s (%s)" % (total_seconds, explain(args, len(photos), len(slides), overlap)))
            return 0

        parent = os.path.dirname(os.path.abspath(args.output))
        if not os.path.isdir(parent):
            raise SlideshowError("the output folder does not exist: %s" % parent)
        tmp = tempfile.mkdtemp(prefix="slideshow-")
        try:
            clips = []
            if args.title is not None:
                log("title card")
                clips.append((render_title_clip(ffmpeg, tmp, args, width, height, slides[0]), slides[0]))
            for i, ph in enumerate(photos):
                log("photo %d/%d  %s  (%s)" % (i + 1, len(photos), os.path.basename(ph), motions[i]))
                out = os.path.join(tmp, "clip_%05d.mp4" % i)
                render_photo_clip(ffmpeg, ph, out, args, width, height, slide_frames_each, motions[i], i)
                clips.append((out, slide_frames_each))
            video, frames = join_in_batches(ffmpeg, clips, tmp, args.batch, overlap,
                                            args.transition, args.fps, args.crf, args.preset, log)
            if args.music:
                log("adding music")
                add_music(ffmpeg, video, args.music, args.output, frames / float(args.fps), args.music_fade)
            else:
                shutil.move(video, args.output)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        info = probe(ffprobe, args.output)
        print("wrote %s" % args.output)
        print("  length:     %.2f s (%s)" % (info["duration"], explain(args, len(photos), len(slides), overlap)))
        print("  size:       %s (%d bytes)" % (human_size(info["size"]), info["size"]))
        print("  resolution: %d x %d" % (info["width"], info["height"]))
        print("  audio:      %s" % (info["audio_codec"] if info["has_audio"] else "none"))
        return 0
    except SlideshowError as exc:
        print("slideshow: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("slideshow: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
