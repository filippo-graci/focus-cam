# focus

Webcam focus guard. It plays music for the chosen mode and nags you when your head turns away from your work screen. When you leave the camera's view, it pauses.

```bash
focus soft        # start in background (or: focus, hard)
focus hard        # while running: switch mode live, no recalibration
focus next        # next track in the current mode's folder (shuffled playlist)
focus prev        # previous track
focus seek +30    # move in the track: +30 / -30 seconds, or a time like 12:30
focus pause       # pause / resume the music (the session keeps tracking)
focus paper       # toggle the paper margin: looking down at your desk (writing notes) stops counting as distracted
focus status      # mode + current track
focus watch       # live view of the running session, refreshed every second (Ctrl+C closes the view only)
focus app         # desktop window (also "Focus" in the app menu): mode switch, start/stop, music player
                  # (timeline: click/drag to seek, prev / -30s / pause / +30s / next), live session,
                  # week + month level (each restarts from 1 every Monday / every 1st), streak, 14-day chart, month/all-time totals, session list
focus stop        # stop + print session summary
focus log         # follow the log
focus hard --preview   # camera window with live angles, q to quit
```

`focus` is a script in `~/.local/bin/focus`. Run `uv run focus.py soft` in this folder to see the live status line in the foreground.

First run: `cp config.example.toml config.toml`.

On start it calibrates for about 3 seconds. **Look at the screen you'll work on**, even if that isn't the one with the camera. Any head turn or tilt beyond `pitch_tolerance` / `yaw_tolerance` from that pose counts as distracted. To recalibrate, restart.

## Music

Put your mixes (mp3/ogg/flac/wav) in `music/soft/`, `music/focus/` and `music/hard/`. Each folder plays shuffled on a loop. mp4/m4a won't play (no AAC decoding in pygame), so convert to mp3 first.

From YouTube (a single video or a whole playlist URL):

```bash
yt-dlp -x --audio-format mp3 -o "~/Workspace/focus/music/soft/%(title)s.%(ext)s" "URL"
```

## Modes

| mode  | nag after | what happens |
|-------|-----------|--------------|
| soft  | 20 s | music drops to 30% + soft chime, volume restored when you look back |
| focus | 8 s  | beep + desktop notification, repeats every 15 s |
| hard  | 3 s  | harsh noise + critical notification + Telegram message, repeats every 5 s |

No face for 3 s pauses the music. It resumes when you're back. All timings are in `config.toml`.

## Telegram (hard mode)

1. Message @BotFather → `/newbot` → copy the token into `config.toml` (or `export FOCUS_TG_TOKEN=...`).
2. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}` into `chat_id`.

## Stats on the phone

Each session appends one line per mode stretch to `sessions.jsonl` (start, end, mode, seconds focused/distracted/away, nags). Stretches shorter than 30 s are skipped. After each session the whole file is uploaded to the learn_ai app (`[sync]` in `config.toml`; the key is read from `~/Workspace/learn_ai/android/local.properties`). There, the 📈 Stats tab shows week/month levels, streak, charts and each session with a time bar and a green / yellow / red dot (80%+ / 60–79% / under 60% on screen). A failed upload is fixed by the next session, because every upload sends the whole file.

## Tuning

Run `uv run focus.py focus --preview` at your real desk. Glancing at your phone mostly moves your **eyes**, not your head, so watch the `eyes` number as well as `pitch`. Look at your phone, note `eyes` and `pitch`, and set `eye_tolerance` / `pitch_tolerance` just below those values. If your face vanishes (`no face`) right after you look down, it still counts as distracted for `lost_seconds`.

**Paper margin.** When you write on paper while reading the screen, run `focus paper` (or press **Paper** in the app). This swaps `pitch_tolerance` / `eye_tolerance` for `paper_pitch_tolerance` / `paper_eye_tolerance` (30° / 0.8). Turning your head sideways still counts. It stays on across mode switches until you toggle it again or restart. While it's on, the phone passes too, so turn it off when you're done writing. To tune it, write with the preview open and set the paper values just above what you see. If your face disappears while you write, it counts as away.

Self-check of the state machine: `uv run test_focus.py`
