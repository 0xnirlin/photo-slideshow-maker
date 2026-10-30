# slideshow

Turn a folder of photos into an MP4 slideshow with ffmpeg. Ken Burns pan and zoom, crossfades, an optional title card and optional music. One Python file, no dependencies.

```
slideshow photos/
slideshow photos/ --seconds 4 --motion zoom-in --transition slideleft
slideshow photos/ --size vertical --title "Summer in Lisbon" --music song.mp3
```

If you searched for "photos to video ffmpeg", "ffmpeg slideshow with fade" or "ffmpeg zoompan xfade", this is the command you were trying to assemble by hand.

## What it does

- Gives every photo a slow Ken Burns move with `zoompan`: `zoom-in`, `zoom-out`, `pan`, `none`, or `random` (the default, which never repeats the same move twice in a row).
- Joins the photos with `xfade` crossfades: `fade`, `slideleft`, `circleopen`, or `none` for hard cuts.
- Outputs `1080p` (1920x1080), `720p` (1280x720), `vertical` (1080x1920) or `square` (1080x1080). Photos that do not match the frame are shown over a blurred copy of themselves (`--fit blur`, the default), on black bars (`--fit contain`) or cropped to fill (`--fit crop`).
- Adds an optional title card, drawn with a small built-in pixel font, so it works even when your ffmpeg has no `drawtext`.
- Adds optional music, cut to the length of the video, looped if it is shorter, and faded out at the end.
- Sorts by name (natural order, so `img2` comes before `img10`) or by the EXIF date inside JPEGs.
- Prints the length, size, resolution and audio of the file it wrote.
- Keeps memory down by rendering one photo at a time and joining the clips in small batches instead of building one giant filter graph.
- Runs on your machine. Nothing is uploaded.

## Install

You need Python 3.8 or newer and ffmpeg (with ffprobe) on your PATH.

```
# ffmpeg
brew install ffmpeg            # macOS
sudo apt install ffmpeg        # Debian, Ubuntu
winget install ffmpeg          # Windows

# slideshow
pipx install git+https://github.com/0xnirlin/photo-slideshow-maker
```

Or skip the install. The tool is the single file `slideshow.py`. Clone the repo and run it directly:

```
git clone https://github.com/0xnirlin/photo-slideshow-maker
cd photo-slideshow-maker
./slideshow photos/
python3 slideshow.py photos/
```

## Examples

```
# Every photo in a folder, 3 seconds each, random motion, fade, 1080p
slideshow photos/

# Pick the photos yourself, in your order
slideshow cover.jpg a.jpg b.jpg c.jpg -o trip.mp4

# 5 seconds each, always zoom in, slide between photos
slideshow photos/ --seconds 5 --motion zoom-in --transition slideleft

# Vertical video, photos cropped to fill the screen
slideshow photos/ --size vertical --fit crop

# Oldest photo first, using the EXIF date
slideshow photos/ --sort date

# Title card, then music that fades out at the end
slideshow photos/ --title "Summer in Lisbon" --music song.mp3

# Hard cuts, no motion
slideshow photos/ --transition none --motion none

# Just print the order and the length, render nothing
slideshow photos/ --seconds 4 --dry-run
```

Real output from `slideshow photos/ --size 720p` on three photos:

```
photo 1/3  p1.png  (zoom-in)
photo 2/3  p2.png  (zoom-out)
photo 3/3  p3.png  (pan)
joining 3 clip(s)
wrote slideshow.mp4
  length:     7.40 s (3 photo(s) x 3 s, minus 2 overlap(s) of 0.80 s)
  size:       995.0 KB (1018843 bytes)
  resolution: 1280 x 720
  audio:      none
```

### All options

| Option | Meaning |
| --- | --- |
| `-o, --output FILE` | Output file. Default `slideshow.mp4`. |
| `--overwrite` | Replace the output file if it exists. |
| `--seconds N` | Seconds each photo is on screen, overlaps included. Default 3. |
| `--motion` | `zoom-in`, `zoom-out`, `pan`, `none` or `random`. Default `random`. |
| `--zoom-strength N` | How far the zoom goes, 0.15 means 15 percent. Default 0.15. |
| `--transition` | `fade` (default), `slideleft`, `circleopen` or `none`. |
| `--transition-seconds N` | Overlap between photos. Default 0.8. At most half of `--seconds`. |
| `--size` | `1080p` (default), `720p`, `vertical` or `square`. |
| `--fit` | `blur` (default), `contain` or `crop`. |
| `--fps N` | Frames per second. Default 30. |
| `--sort` | `auto` (default), `name` or `date`. See below. |
| `--title TEXT` | Add a title card as the first slide. |
| `--title-seconds N` | Title card length. Default 3. |
| `--title-bg`, `--title-color` | Title colors as `#rrggbb`. Defaults `#111111` and `#ffffff`. |
| `--music FILE` | Audio to play under the slideshow. Any format ffmpeg reads. |
| `--music-fade N` | Fade-out length in seconds, 0 for none. Default 2. |
| `--batch N` | Clips joined per ffmpeg call. Lower uses less memory. Default 6 up to 720p, 2 above. |
| `--threads N` | x264 threads per ffmpeg call, 0 lets ffmpeg decide. Default 2. |
| `--crf N`, `--preset NAME` | x264 quality (default 18) and speed preset (default `medium`). |
| `--seed N` | Seed for `--motion random`, so a run can be repeated. Default 1. |
| `--dry-run` | Print the order and the length, render nothing. |

Exit codes: 0 on success, 1 for an error (no photos, missing file, no ffmpeg, ffmpeg failed), 2 for bad options.

## How long will it be?

```
length = (photos x seconds) + title seconds - (slides - 1) x transition seconds
```

`slides` is the number of photos, plus one if you add a title. Each photo gets `--seconds` of screen time and every join eats `--transition-seconds` from that. With `--transition none` there is no overlap, so it is just photos x seconds.

| Command | Math | Measured |
| --- | --- | --- |
| 3 photos, `--seconds 3`, default fade 0.8 | 3 x 3 - 2 x 0.8 | 7.40 s |
| 3 photos, `--seconds 3`, 3 s title, fade 0.8 | 3 x 3 + 3 - 3 x 0.8 | 9.60 s |
| 12 photos, `--seconds 3`, fade 0.8 | 12 x 3 - 11 x 0.8 | 27.20 s |

Seconds are rounded to whole frames, so at 30 fps the real length can differ from the formula by a frame or two. `--dry-run` prints the number before you render.

## Sorting and EXIF dates

- `--sort auto` (default): photos inside a folder are sorted by name, photos you list on the command line keep the order you typed.
- `--sort name`: everything sorted by name, natural order.
- `--sort date`: everything sorted by the EXIF `DateTimeOriginal` of each JPEG. A file with no EXIF date, such as a PNG or a screenshot, falls back to its modified time.

The EXIF date is read by a small parser in the script (JPEG only), with no extra libraries. PNG and WebP metadata is not read.

## Memory and speed

Each photo is rendered alone into a short clip, then the clips are joined with `xfade` a few at a time, and the results are joined again. A 12 photo set never needs one filter graph with 12 inputs, and a bigger set is joined in levels, not all at once (tested with 12 photos at `--batch 4`, which takes two levels). The cost is that photos in the middle are re-encoded once per level, at high quality (CRF 14) before the final encode.

Measured on an Apple M2 Mac, ffmpeg 8.0.1, with the largest ffmpeg process as reported by `/usr/bin/time -l`:

| Run | Wall time | Peak memory |
| --- | --- | --- |
| 3 photos, `--size 720p` | 5.4 s | 537 MB |
| 3 photos, 1080p, title and music, default batch | 15.4 s | 881 MB |
| 12 photos, hidden 160x90 test size, `--batch 4` | 1.7 s | 93 MB |

The photos in these runs were 320x180 PNGs scaled up to the output size, so they say little about how long real 12 megapixel photos take to decode. If memory is tight, use `--size 720p`, `--batch 2` and `--threads 1`.

## FAQ

**Which photo formats work?**
JPEG, PNG, WebP, BMP and TIFF. Whatever your ffmpeg can decode from those extensions.

**Why can't the transition be longer?**
The overlap can be at most half of `--seconds`. Otherwise one photo would be fading in and out at the same time. The tool tells you if you go over.

**Does `--seconds` include the crossfade?**
Yes. A photo is on screen for `--seconds` in total, and the first and last `--transition-seconds` of that are shared with its neighbours.

**What if my music is shorter than the slideshow?**
It loops. If it is longer, it is cut at the end of the video. The fade-out is applied to the end either way, and `--music-fade 0` turns it off.

**Why does the title look blocky?**
It uses a tiny built-in 5x7 pixel font so it works with any ffmpeg build, including ones without `drawtext` (the one used for testing this tool had none). It draws capital letters, digits and basic punctuation, and accents are stripped. For a designed title, make a PNG in any editor and put it first in the list.

**Do portrait photos get rotated correctly?**
Portrait photos made as portrait pixel data work (tested). Photos that rely on an EXIF rotation flag depend on what your ffmpeg version does with that flag. This tool does not read or apply it itself, and I did not test it.

**Can I mix portrait and landscape photos?**
Yes. `--fit blur` (default) and `--fit contain` keep each photo whole, `--fit crop` fills the frame and cuts the edges.

**Is the Ken Burns move smooth?**
Each photo is placed on a frame twice the output size before `zoompan` moves over it, which is the usual way to reduce the pixel stepping `zoompan` can show on slow moves. I only looked at single frames, not at motion on a large screen, so judge it on your own photos.

**The file is too big.**
Raise `--crf` (for example 23), use `--preset slow`, pick `--size 720p`, or lower `--fps`.

**It says ffmpeg is not found.**
Install ffmpeg (see Install) and open a new terminal so the PATH updates. Check with `ffmpeg -version` and `ffprobe -version`.

**Which Python?**
3.8 or newer. There are no third-party packages.

## Prefer a browser?

If you do not want to install anything, there is [an AI Slideshow Maker in the browser](https://getziztu.com/tools/ai-slideshow-maker?utm_source=github&utm_medium=readme&utm_campaign=photo-slideshow-maker). Its Classic mode (pan and zoom, a music track) is free with a free account, and its AI motion mode, which animates each photo, uses credits that are shown before you start.

## Development

```
python3 -m unittest discover -s tests -v
```

The tests generate three 320x180 PNGs and a one second sine tone with ffmpeg, render with the hidden `--test-size` flag so every video is tiny, and check the length against the formula (fade, cuts, title, other transitions, batching), the resolution for each `--size`, that `--music` adds an audio track cut to the video's length, EXIF date sorting, and the error exit codes. They need ffmpeg on your PATH.

## License

MIT. Copyright (c) 2026 Ziztu.
