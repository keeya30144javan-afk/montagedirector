# Montage Director

Make AMVs, GMVs and montages the way 2000s and early-2010s YouTube edits looked: cuts that hit the beat, white flashes on the big hits, zoom punches, shake, glitches, glow and ghost trails. Add your footage and a song, describe the edit you have in your head, and Montage Director builds it.

## Download

Get the latest installer from the **[Releases page](https://github.com/keeya30144javan-afk/montagedirector/releases/latest)**:

- **Windows:** `MontageDirector-Setup-x.y.z.exe`. Run it; it adds a desktop shortcut. If Windows says "Windows protected your PC", click **More info**, then **Run anyway** (the installer isn't code-signed yet).
- **Mac:** `MontageDirector-x.y.z-mac.dmg`. Drag the app to Applications. The first time, right-click it and choose **Open** (the app isn't notarized yet).

The app checks for updates when it starts and offers to download new versions. Your settings and exports are kept when you update.

## How to make an edit

1. **Footage:** drop in episodes, gameplay recordings or clips (MP4, MKV, MOV, WebM and more). Longer footage gives more variety.
2. **Song:** drop in an MP3 (or WAV, M4A, FLAC...). Use *Use part of it* to edit only a section, like 0:45 to 1:30.
3. **Style:** pick a starting point: Hype AMV/GMV, 2000s Flash Edit, Sad / Emotional, Cinematic Trailer, Glitch / VHS or Clean Beat Sync.
4. **Your idea:** describe the edit, for example *"Slow emotional intro on the quiet scenes, then go crazy on the drop with the fight scenes and lots of flashes. Freeze frame at 1:05. Vertical for TikTok."*
5. **Director:**
   - **Free (on this PC):** no account needed. Understands keywords: fast, slow, calm, crazy, flashes, shake, zoom, glitch, slow mo, reverse, strobe, vertical, square, black and white, VHS, Y2K, glow, trails, letterbox, and times like "freeze at 1:05" or "flash at 0:42". You can aim words at parts of the song: "calm intro", "crazy drop".
   - **AI Director:** Claude looks at your footage, reads your idea in plain English and chooses which scenes go where. Needs your own Anthropic API key (Settings, then paste the key from [console.anthropic.com](https://console.anthropic.com/settings/keys)). You pay Anthropic directly; the cost of each edit is shown in the app.
6. Press **Make Edit**. A preview plays when it's ready.
7. **Refine** it in your own words ("more flashes in the drop", "use the rooftop scene at the end"), or press **New variation** for different shot choices with the same idea.
8. **Export video** saves the full-quality MP4 to `Documents/Montage Director/Exports`.

## What it does under the hood

- **Watches the footage:** finds every scene cut, scores each shot for motion, brightness and colour, skips black screens, and crops black bars baked into old 4:3 footage.
- **Listens to the song:** tempo, beats, bars, sections (intro, verse, build, drop, breakdown, outro), drops and the strongest hits.
- **Directs:** the Free Director or the AI Director decides pacing, shot choice and effects for each part of the song.
- **Plans:** every cut is snapped to an exact video frame on the beat, so the edit never drifts out of sync.
- **Renders with FFmpeg:** each cut is encoded with its effects in parallel and cached, so refining an edit only re-renders the cuts that changed.

## Command line

```
python -m montage_director make --footage path/to/clips --song song.mp3 --style hype \
    --prompt "calm intro, crazy drop, vertical" --start 0:45 --end 1:30 --out edit.mp4
```

Add `--ai` to use the AI Director (reads `ANTHROPIC_API_KEY`), `--preview` for a fast low-resolution render, and `--seed N` to repeat an edit exactly.

## Run from source

Requires Python 3.10 or newer.

```
pip install -r requirements.txt
python run_app.py
```

Tests: `pip install pytest` then `pytest`.

## Releasing a new version

1. Update `__version__` in `montage_director/__init__.py`.
2. Push a tag: `git tag v0.2.0 && git push origin v0.2.0`.
3. GitHub Actions builds the Windows installer and the Mac disk image and publishes them on the Releases page. Installed copies see the update the next time they start.

To build test installers without publishing, open **Actions → Build installers → Run workflow**.

## Your footage and music

Montage Director is an editing tool, like any video editor. You're responsible for having the rights to the footage and music you use, and for following the rules of the sites you post to. Edits that use copyrighted songs or footage may be claimed or blocked when uploaded.

## Third-party software

Montage Director bundles open-source components, including [FFmpeg](https://ffmpeg.org) (via imageio-ffmpeg, GPL build), [Qt for Python / PySide6](https://www.qt.io/qt-for-python) (LGPL), [librosa](https://librosa.org) (ISC), NumPy, SciPy and Pillow. Their licenses apply to those components.
