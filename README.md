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
   - **Free (on this PC):** no account needed. Reads your idea the way an editor would: pace ("a little faster", "not too fast", "back to the flow", "every 2 beats"), effects with amounts ("a few flashes", "less shake", "no glitch"), the drums ("cut on the kick", "flash on every snare", "slow motion on the big hits"), what to show ("fight scenes", "explosions", "close-ups", "dark scenes", "blue shots"), and where ("the chorus", "the second drop", "from 0:30 to 0:45", "the last 20 seconds", "halfway through", "when the solo starts", "before each drop"). Press **Ideas** for examples and **What can I say?** for the full list. Anything it doesn't understand is shown as *Didn't catch* so you can rephrase it.
   - **AI Director:** Claude looks at your footage, reads your idea in plain English and chooses which scenes go where. Needs your own Anthropic API key (Settings, then paste the key from [console.anthropic.com](https://console.anthropic.com/settings/keys)). You pay Anthropic directly; the cost of each edit is shown in the app.
6. Press **Make Edit**. A preview plays when it's ready. Under the timeline, **Feel:** describes the song (tempo, whether it has a steady drum beat, how drum-driven it is), **Heard:** lists the moments the app heard (quiet intro, drums getting faster, solo, hard hits, the last note ringing out) and **Your timeline:** shows where each of your instructions landed. Cut lengths follow the song: very fast songs cut every other beat, songs without drums cut on phrases, and the calmer stretches of a part cut a little slower than its peaks (an exact "every beat" in your idea is always kept).
7. Fine-tune it:
   - **Refine** the whole edit in your own words ("more flashes in the drop", "cut every 2 beats").
   - **Refine one part:** drag across the timeline to select it, type what should happen there ("quicker cuts with punches", "hold one shot", "cut on the hits"), press Refine. Right-click a green section box to undo it.
   - **Pick shot…:** select part of the timeline and press **Pick shot…** to choose exactly which shot plays there from thumbnails, for example a group shot for the last ringing note. Tick *Freeze on this frame* to show it as a still. Ctrl+click several shots to play them in order on the cuts already in that part (your taps stay where they are).
   - **Zoom the timeline:** mouse wheel over the timeline zooms in and out, Shift + wheel scrolls, or use the − / + / Fit buttons. Zoomed in, the beat grid is drawn so you can place things exactly.
   - **Tap to Direct:** press it and tap along with the music: Space = cut, F = flash, Z = zoom, X = shake, D = drop, Backspace = undo. Taps snap to the nearest drum hit. Press **Done tapping** and the edit is rebuilt around your taps. To take out one tap later, right-click it on the timeline and choose **Remove this tap** (or select a stretch and remove all the taps in it).
   - **Fix a drop:** right-click the timeline where it really hits and choose **Mark the drop here**.
   - **New variation** keeps the plan but picks different shots.
   - **Undo / Redo** (↶ ↷, or Ctrl+Z / Ctrl+Y) steps back through every change you made.
   - **Before / After** (or press C) flips the player between this version and the one before your last change, at the same moment in the song.
8. **Export video** saves the full-quality MP4 to `Documents/Montage Director/Exports`. Pick a preset next to the button: High quality, Best quality, YouTube 1080p60, TikTok / Shorts / Reels (vertical, follows the action) or Discord (kept under 25 MB).
9. **File → Save project** (Ctrl+S) keeps everything (footage, song, idea, section edits, taps, drop marks) in a `.mdproj` file in `Documents/Montage Director/Projects`. **File → Open project** brings it back exactly as it was. Your API key is never saved in project files.

### Writing your idea as a timeline

You can describe the whole song in order, with times or with musical moments:

> The beginning should be one still shot during the cymbals, and as soon as the bass drops the action starts. At 1:36 when the drums get faster, quicker cuts with punches. At 2:30 back to the flow. Around 2:50 when the guitar solo starts, slower cuts. Around 3:57 the hard drums hitting 1, 2, 3, 4: cut on each hit. At the end when the last note rings, hold one shot of him posing.

Each instruction runs until the next one (or until that part of the song ends). Rough times are moved to the moment the app hears nearby, so "around 2:50 when the solo starts" lands where the solo really begins.

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
2. Push a tag: `git tag v0.4.2 && git push origin v0.4.2`.
3. GitHub Actions builds the Windows installer and the Mac disk image and publishes them on the Releases page. Installed copies see the update the next time they start.

To build test installers without publishing, open **Actions → Build installers → Run workflow**.

## Your footage and music

Montage Director is an editing tool, like any video editor. You're responsible for having the rights to the footage and music you use, and for following the rules of the sites you post to. Edits that use copyrighted songs or footage may be claimed or blocked when uploaded.

## Third-party software

Montage Director bundles open-source components, including [FFmpeg](https://ffmpeg.org) (via imageio-ffmpeg, GPL build), [Qt for Python / PySide6](https://www.qt.io/qt-for-python) (LGPL), [librosa](https://librosa.org) (ISC), NumPy, SciPy and Pillow. Their licenses apply to those components.
