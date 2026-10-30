"""Tests for slideshow. They build tiny media with ffmpeg (320x180 PNGs, a
sine tone) and render with the hidden --test-size flag, so every video is
tiny. They need ffmpeg and ffprobe on your PATH."""

import contextlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import slideshow  # noqa: E402

FPS = 10
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y"] + list(args), check=True)


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height", "-of", "json", path],
        stdout=subprocess.PIPE, check=True).stdout
    info = json.loads(out)
    video = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    return float(info["format"]["duration"]), video["width"], video["height"], has_audio


def exif_jpeg(src_jpg, dst_jpg, stamp):
    """Copy a JPEG and insert an EXIF block whose DateTimeOriginal is stamp."""
    tiff = (b"MM\x00*\x00\x00\x00\x08"
            + struct.pack(">H", 1) + struct.pack(">HHII", 0x8769, 4, 1, 26) + b"\x00\x00\x00\x00"
            + struct.pack(">H", 1) + struct.pack(">HHII", 0x9003, 2, 20, 44) + b"\x00\x00\x00\x00"
            + stamp.encode("ascii") + b"\x00")
    data = b"Exif\x00\x00" + tiff
    seg = b"\xff\xe1" + struct.pack(">H", len(data) + 2) + data
    with open(src_jpg, "rb") as f:
        raw = f.read()
    with open(dst_jpg, "wb") as f:
        f.write(raw[:2] + seg + raw[2:])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are needed")
class SlideshowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="slideshow-test-")
        cls.photos = os.path.join(cls.tmp, "photos")
        os.mkdir(cls.photos)
        sources = ["testsrc2=size=320x180:rate=1", "mandelbrot=size=320x180:rate=1",
                   "gradients=size=320x180:rate=1:seed=3"]
        for i, src in enumerate(sources, 1):
            ffmpeg("-f", "lavfi", "-i", src, "-frames:v", "1", os.path.join(cls.photos, "p%d.png" % i))
        cls.p = [os.path.join(cls.photos, "p%d.png" % i) for i in (1, 2, 3)]
        cls.portrait = os.path.join(cls.tmp, "portrait.png")
        ffmpeg("-f", "lavfi", "-i", "testsrc2=size=180x320:rate=1", "-frames:v", "1", cls.portrait)
        cls.tone = os.path.join(cls.tmp, "tone.wav")
        ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", cls.tone)  # 1 s, shorter than the video

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, *argv, expect=0):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = slideshow.main(list(argv))
            except SystemExit as e:  # argparse errors
                code = e.code
        self.assertEqual(code, expect, err.getvalue())
        return out.getvalue(), err.getvalue()

    def render(self, name, *argv):
        path = os.path.join(self.tmp, name)
        self.run_cli(*(["--transition-seconds", "0.4"] + list(argv)
                       + ["-o", path, "--overwrite", "--test-size", "--fps", str(FPS)]))
        return path

    # --- duration math: photos x seconds minus overlaps ----------------------

    def test_duration_fade(self):
        path = self.render("fade.mp4", self.photos, "--seconds", "2", "--transition-seconds", "0.5")
        dur, _, _, _ = probe(path)
        self.assertAlmostEqual(dur, 3 * 2 - 2 * 0.5, delta=0.11)  # 5.0

    def test_duration_no_transition(self):
        path = self.render("cut.mp4", self.photos, "--seconds", "2", "--transition", "none")
        self.assertAlmostEqual(probe(path)[0], 6.0, delta=0.11)

    def test_duration_with_title(self):
        path = self.render("title.mp4", self.photos, "--seconds", "2", "--transition-seconds", "0.5",
                           "--title", "Hello", "--title-seconds", "2")
        self.assertAlmostEqual(probe(path)[0], 4 * 2 - 3 * 0.5, delta=0.11)  # 6.5

    def test_duration_other_transitions(self):
        for name in ("slideleft", "circleopen"):
            path = self.render(name + ".mp4", self.p[0], self.p[1], "--seconds", "1",
                               "--transition", name, "--transition-seconds", "0.5")
            self.assertAlmostEqual(probe(path)[0], 1.5, delta=0.11, msg=name)

    def test_dry_run_matches_formula(self):
        out, _ = self.run_cli(self.photos, "--seconds", "4", "--transition-seconds", "1", "--dry-run")
        self.assertIn("length: 10.00 s", out)  # 3 x 4 - 2 x 1

    def test_batching_keeps_the_formula(self):
        many = os.path.join(self.tmp, "many")
        os.mkdir(many)
        for i in range(7):
            shutil.copy(self.p[i % 3], os.path.join(many, "m%02d.png" % i))
        path = self.render("many.mp4", many, "--seconds", "1", "--transition-seconds", "0.4",
                           "--batch", "2")  # 7 -> 4 -> 2 -> 1: three levels
        self.assertAlmostEqual(probe(path)[0], 7 * 1 - 6 * 0.4, delta=0.15)  # 4.6

    # --- resolution ------------------------------------------------------------

    def test_sizes(self):
        expect = {"1080p": (160, 90), "720p": (106, 60), "vertical": (90, 160), "square": (90, 90)}
        for size, wh in expect.items():
            path = self.render("size_%s.mp4" % size, self.p[0], "--seconds", "1", "--size", size)
            _, w, h, _ = probe(path)
            self.assertEqual((w, h), wh, size)
        self.assertEqual(slideshow.output_size("1080p"), (1920, 1080))
        self.assertEqual(slideshow.output_size("vertical"), (1080, 1920))
        self.assertEqual(slideshow.output_size("square"), (1080, 1080))
        self.assertEqual(slideshow.output_size("720p"), (1280, 720))

    def test_fits_and_motions_render(self):
        for fit in slideshow.FITS:
            path = self.render("fit_%s.mp4" % fit, self.portrait, "--seconds", "1", "--fit", fit)
            self.assertEqual(probe(path)[1:3], (160, 90), fit)
        for motion in slideshow.MOTIONS:
            path = self.render("motion_%s.mp4" % motion, self.p[0], self.p[1], "--seconds", "1",
                               "--motion", motion, "--transition-seconds", "0.4")
            self.assertAlmostEqual(probe(path)[0], 1.6, delta=0.11, msg=motion)

    # --- audio -------------------------------------------------------------------

    def test_no_music_means_no_audio(self):
        path = self.render("silent.mp4", self.p[0], "--seconds", "1")
        self.assertFalse(probe(path)[3])

    def test_music_is_added_and_trimmed_to_the_video(self):
        path = self.render("music.mp4", self.photos, "--seconds", "2", "--transition-seconds", "0.5",
                           "--music", self.tone)
        dur, _, _, has_audio = probe(path)
        self.assertTrue(has_audio)
        self.assertAlmostEqual(dur, 5.0, delta=0.15)  # 1 s tone, looped and cut to the video
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration",
             "-of", "csv=p=0", path], stdout=subprocess.PIPE).stdout.decode().strip()
        self.assertAlmostEqual(float(out), 5.0, delta=0.2)

    # --- sorting -------------------------------------------------------------------

    def test_natural_name_sort(self):
        d = os.path.join(self.tmp, "natural")
        os.mkdir(d)
        for n in ("img10.png", "img2.png", "img1.png"):
            shutil.copy(self.p[0], os.path.join(d, n))
        got = [os.path.basename(x) for x in slideshow.collect_photos([d])]
        self.assertEqual(got, ["img1.png", "img2.png", "img10.png"])

    def test_exif_date_sort(self):
        d = os.path.join(self.tmp, "exif")
        os.mkdir(d)
        base = os.path.join(self.tmp, "base.jpg")
        ffmpeg("-i", self.p[0], "-frames:v", "1", base)
        exif_jpeg(base, os.path.join(d, "a_new.jpg"), "2024:06:01 10:00:00")
        exif_jpeg(base, os.path.join(d, "b_old.jpg"), "2019:01:02 03:04:05")
        exif_jpeg(base, os.path.join(d, "c_mid.jpg"), "2022:12:31 23:59:59")
        self.assertEqual(slideshow.read_exif_datetime(os.path.join(d, "b_old.jpg")), "2019:01:02 03:04:05")
        self.assertIsNone(slideshow.read_exif_datetime(self.p[0]))  # a PNG has none
        by_name = [os.path.basename(x) for x in slideshow.collect_photos([d], "name")]
        by_date = [os.path.basename(x) for x in slideshow.collect_photos([d], "date")]
        self.assertEqual(by_name, ["a_new.jpg", "b_old.jpg", "c_mid.jpg"])
        self.assertEqual(by_date, ["b_old.jpg", "c_mid.jpg", "a_new.jpg"])
        path = self.render("exif.mp4", d, "--sort", "date", "--seconds", "1", "--transition", "none")
        self.assertAlmostEqual(probe(path)[0], 3.0, delta=0.11)  # the EXIF JPEGs decode fine

    def test_listed_files_keep_their_order(self):
        got = slideshow.collect_photos([self.p[2], self.p[0]])
        self.assertEqual(got, [self.p[2], self.p[0]])

    # --- errors ----------------------------------------------------------------------

    def test_errors(self):
        self.run_cli(os.path.join(self.tmp, "nope"), expect=1)
        self.run_cli(self.photos, "--seconds", "1", "--transition-seconds", "0.8", expect=2)
        self.run_cli(self.photos, "--music", os.path.join(self.tmp, "missing.mp3"), expect=1)
        existing = self.render("exists.mp4", self.p[0], "--seconds", "1")
        self.run_cli(self.p[0], "-o", existing, "--test-size", expect=1)  # no --overwrite


class UnitTests(unittest.TestCase):
    def test_expected_frames(self):
        self.assertEqual(slideshow.expected_frames([60, 60, 60], 15), 150)
        self.assertEqual(slideshow.expected_frames([60], 15), 60)
        self.assertEqual(slideshow.expected_frames([90, 60, 60], 0), 210)

    def test_to_frames(self):
        self.assertEqual(slideshow.to_frames(0.8, 30), 24)
        self.assertEqual(slideshow.to_frames(0.001, 30), 1)

    def test_title_ppm_has_the_right_size(self):
        path = os.path.join(tempfile.mkdtemp(prefix="slideshow-ppm-"), "t.ppm")
        try:
            slideshow.write_title_ppm(path, "Hello, Wörld!", 160, 90, (0, 0, 0), (255, 255, 255))
            with open(path, "rb") as f:
                data = f.read()
            header = b"P6\n160 90\n255\n"
            self.assertTrue(data.startswith(header))
            self.assertEqual(len(data), len(header) + 160 * 90 * 3)
            self.assertIn(255, data[len(header):])  # some text was drawn
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_random_motion_never_repeats_in_a_row(self):
        m = slideshow.pick_motions("random", 40, 1)
        self.assertTrue(all(a != b for a, b in zip(m, m[1:])))
        self.assertEqual(slideshow.pick_motions("pan", 3, 1), ["pan"] * 3)


if __name__ == "__main__":
    unittest.main()
