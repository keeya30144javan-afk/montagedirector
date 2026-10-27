# Contributing to Montage Director

Thanks for helping! This page is the map: what each file does, how an edit gets made, and
how to change things without breaking them.

## Get it running

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt pytest
python run_app.py          # the app
pytest -q                  # all tests (about a minute; they render real tiny videos)
pytest -q tests/test_reader.py   # just the prompt reader (about a second)
```

No API key is needed. The free director works offline and everything in the tests uses it.

## How an edit gets made

```
footage ──► footage.py ──► shots (motion, colour, camera moves, impacts)
song ─────► music.py + listening.py ──► SongMap (beats, bars, sections, drops, drum hits, feel)
prompt ───► reader/ (free) or director_ai.py (Claude) ──► Direction (what the user wants)
                    │
                    ▼
           planner.py: Direction + SongMap + shots ──► EditPlan (every cut, frame-exact)
                    │
                    ▼
           render.py (+ titles.py for text cards) ──► ffmpeg ──► the video
```

`project.py` ties these steps together for one project: it holds the inputs and settings, runs
each step, and saves and loads `.mdproj` files. The app and the command line (`cli.py`) both
just drive a `Project`.

## Where things live

| File | What it does |
|---|---|
| `footage.py` | Splits videos into shots and scores each one (brightness, motion, camera moves, text on screen). |
| `music.py` | Tempo, beats, bars, song sections, drops and hits. |
| `listening.py` | Instrument lanes and musical moments ("the solo starts", "the music stops"), plus the song's feel. |
| `direction.py` | The styles (Hype, Cinematic…) and the `Direction` object that describes the wanted edit. |
| `reader/` | **The free prompt reader.** It turns plain English into a `Direction` (see below). |
| `director_ai.py` | The optional AI Director (it uses the user's own Claude key). |
| `planner.py` | Picks a shot for every cut and adds effects, sync, holds, transitions and text. |
| `plan.py` | The `EditPlan` data: cuts, effects and the look. |
| `render.py`, `titles.py`, `ffmpeg_tools.py` | Turn the plan into video with ffmpeg. |
| `project.py` | One project end to end: taps, regions, favourite/banned shots, save/load. |
| `paths.py`, `updates.py` | Data folders and the "new version available" check. |

### The prompt reader (`montage_director/reader/`)

It reads a prompt in four steps, one file each:

1. `normalise.py` fixes typos and turns "1 minute 30" into 1:30, "0:30-0:45" into a range and "last 20 seconds" into a span.
2. `splitting.py` splits the prompt into instructions at sentences, commas, "then", and "and" before a new part of the song.
3. `instruction.py` reads one instruction: **where** it applies and **what** it asks for.
4. `apply.py` builds the `Direction`: the whole edit first, then parts of the song, then timed sections, so the more specific instruction wins.

Three more files sit beside them. `vocabulary.py` has every word list, `spelling.py` has the "did you mean…?" suggestions, and `typing_help.py` has the speech-bubble tips.

**To teach it a new phrase:**
1. Add the phrase to `tests/phrasings.py`, along with the words you expect in the reading.
2. Run `pytest -q tests/test_reader.py` and watch it fail.
3. Add the words to the right list in `vocabulary.py`. That list is `FAST`, `SLOW`, `HOLD`, `CONTENT`, `EFFECT_WORDS` or `GLOBAL_HINTS`.
4. Run the tests again.

The roughly 200 phrasings in that file are how real people type. Each must keep reading correctly, so a fix for one wording can't quietly break another.

### The app (`montage_director/app/`)

The `MainWindow` class is split by job. Each file adds methods to the same window:

| File | Part of the window |
|---|---|
| `main_window.py` | Start-up, running jobs (make / refine / export), results, undo/redo, help, `main()` |
| `window_layout.py` | Building the menus, toolbar, sidebar and main area |
| `window_files.py` | New / open / save / recent projects, autosave, crash recovery |
| `window_timeline.py` | Tap to Direct, selected parts, markers, drops, shot picking, text cards, scene keys |
| `window_preview.py` | Video preview, play/seek, before/after compare, previewing one selected part |
| `dialogs.py` | Pop-ups: settings, shot picker, instruction box, text cards, scene keys |
| `widgets.py` | Custom widgets: the timeline, drop lists, style grid, speech bubble |
| `common.py` | Shared helpers, constants and help texts |
| `theme.py`, `store.py`, `worker.py` | Colours/stylesheet, saved settings, background jobs |

Slow work, like analysis, planning and rendering, runs off the main thread through `worker.start_job`, so the window never freezes.

## Before you send a change

- `pytest -q` passes. CI runs the same tests on every push.
- If you change how the free director reads prompts, add the prompt to `tests/phrasings.py`.
- `tests/test_obedience.py` makes an edit for each instruction with every style and **measures** the result (shot lengths, effects, transitions, holds). If you add something people can ask for, add a check there too: reading a prompt right isn't enough if the edit doesn't follow it.
- Keep the code style of the file you're in. Keep the text people see in plain English.
- If a cached result changes shape, bump its version number so old caches are rebuilt. The version numbers are `ANALYSIS_VERSION`, `MUSIC_VERSION`, `RENDER_VERSION`, `TAG_VERSION` and `PROJECT_VERSION`.

Releases are made by pushing a `vX.Y.Z` tag (see the README). GitHub Actions builds the
Windows and Mac installers.
