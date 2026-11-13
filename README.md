# focus-cam

Webcam focus guard. It plays music for the chosen mode and nags when the head turns away from the work screen. Leaving the camera's view pauses it.

The camera doesn't need to face the work screen. It can sit off center or on a secondary monitor: calibration records the head pose while looking at the work screen, and tracking measures turns from that pose.

```bash
focus soft        # start in background (or: focus, hard)
focus hard        # while running: switch mode live, no recalibration
focus next        # next track in the current mode's folder (shuffled playlist)
focus prev        # previous track
focus seek +30    # move in the track: +30 / -30 seconds, or a time like 12:30
focus pause       # pause / resume the music (the session keeps tracking)
focus paper       # toggle the paper margin: looking down at the desk (writing notes) stops counting as distracted
focus status      # mode + current track
focus watch       # live view of the running session, refreshed every second (Ctrl+C closes the view only)
focus app         # desktop window (also "Focus" in the app menu): mode switch, start/stop, music player
                  # (timeline: click/drag to seek, prev / -30s / pause / +30s / next), live session,
                  # week + month level (each restarts from 1 every Monday / every 1st), streak, 14-day chart, month/all-time totals, session list
focus stop        # stop + print session summary
focus log         # follow the log
focus hard --preview   # camera window with live angles, q to quit
```

`focus` is a script in `~/.local/bin/focus`. Run `uv run focus.py soft` in this folder to see the live status line in the foreground. Before the first run, copy `config.example.toml` to `config.toml`.

On start it calibrates for about 3 seconds. During that time, look at the work screen, even if the camera is on a different one. Any head turn or tilt beyond `pitch_tolerance` / `yaw_tolerance` from that pose counts as distracted. To recalibrate, restart.

## Music

Put mixes (mp3/ogg/flac/wav) in `music/soft/`, `music/focus/` and `music/hard/`. Each folder plays shuffled on a loop. mp4/m4a won't play (no AAC decoding in pygame), so convert to mp3 first.

From YouTube (a single video or a whole playlist URL):

```bash
yt-dlp -x --audio-format mp3 -o "~/Workspace/focus/music/soft/%(title)s.%(ext)s" "URL"
```

## Modes

| mode  | nag after | what happens |
|-------|-----------|--------------|
| soft  | 20 s | music drops to 30% + soft chime, volume restored on looking back |
| focus | 8 s  | beep + desktop notification, repeats every 15 s |
| hard  | 3 s  | harsh noise + critical notification + Telegram message, repeats every 5 s |

No face for 3 s pauses the music. It resumes when the face returns. All timings are in `config.toml`.

## Telegram (hard mode)

1. Message @BotFather, send `/newbot`, and copy the returned token into `config.toml` (or `export FOCUS_TG_TOKEN=...`).
2. Send the bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": ...}` into `chat_id`.

## Stats on the phone

Each session appends one line per mode stretch to `sessions.jsonl` (start, end, mode, seconds focused/distracted/away, nags). Stretches shorter than 30 s are skipped. After each session the whole file goes up to the learn_ai app (`[sync]` in `config.toml`; the key comes from `~/Workspace/learn_ai/android/local.properties`). If an upload fails, the next session makes up for it, since every upload sends the whole file.

In the app, the 📈 Stats tab shows week and month levels, the streak, charts, and every session with a time bar. Each session also gets a dot for time on screen: green at 80% or more, yellow at 60 to 79%, red below 60%.

## Tuning

Run `uv run focus.py focus --preview` at the real desk. A glance at the phone moves the eyes more than the head, so watch the `eyes` number as well as `pitch`. Look at the phone, note `eyes` and `pitch`, and set `eye_tolerance` / `pitch_tolerance` just below those values. If the face vanishes (`no face`) right after looking down, it still counts as distracted for `lost_seconds`.

### Paper margin

For taking notes on paper while reading the screen, run `focus paper` (or press Paper in the app). This swaps `pitch_tolerance` / `eye_tolerance` for `paper_pitch_tolerance` / `paper_eye_tolerance` (30° / 0.8). Turning the head sideways still counts. It stays on across mode switches until toggled again or restarted. While it's on, the phone passes too, so turn it off after writing. To tune it, write with the preview open and set the paper values just above the readings. If the face disappears while writing, it counts as away.

`uv run test_focus.py` runs a self-check of the state machine.
