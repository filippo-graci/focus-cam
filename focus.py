# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["mediapipe>=0.10.14", "pygame>=2.5"]
# ///
"""Focus guard: webcam head-pose tracking + per-mode music and nags.

Usage: uv run focus.py soft|focus|hard [--preview]
"""
import argparse
import json
import os
import random
import signal
import statistics
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
HERE = Path(__file__).resolve().parent
MODEL = HERE / "models" / "face_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
             "face_landmarker/float16/1/face_landmarker.task")
MUSIC_EXT = {".mp3", ".ogg", ".flac", ".wav"}
MODE_FILE = HERE / ".mode"  # current mode; `focus <mode>` writes the wanted one here + SIGHUP
CMD_FILE = HERE / ".cmd"    # `focus` appends commands here (mode / seek X / pause / paper), then sends SIGHUP


def take_cmds():
    """Take every queued command line. Rename first: a line appended meanwhile lands in a fresh file."""
    taking = CMD_FILE.with_suffix(".taking")
    try:
        CMD_FILE.rename(taking)
    except FileNotFoundError:
        return []
    lines = taking.read_text().splitlines()
    taking.unlink()
    return [ln.strip() for ln in lines if ln.strip()]


class Tracker:
    """Turns per-frame observations into events: away / back / nag / refocus.

    update(now, off): off=None -> no face, True -> looking away, False -> on screen.
    A face that vanishes right after looking away (head dropped to a phone) stays
    "distracted" for `hold` s before counting as away; from on-screen it's `away` s.
    state: focused | glance (looking away for under `glance` s: typing, the keyboard) | distracted | away.
    """

    def __init__(self, grace, repeat, away, hold, glance):
        self.grace, self.repeat, self.away, self.hold, self.glance = grace, repeat, away, hold, glance
        self.lost_since = self.off_since = None
        self.last_nag = float("-inf")
        self.is_away = self.nagging = self.last_off = False
        self.state = "focused"

    def update(self, now, off):
        if off is None:
            if self.lost_since is None:
                self.lost_since = now
            if now - self.lost_since >= (self.hold if self.last_off else self.away):
                self.state = "away"
                if not self.is_away:
                    self.is_away, self.nagging, self.off_since = True, False, None
                    return "away"
                return None
            if not self.last_off:
                self.state = "away"
                return None
            off = True
        else:
            self.lost_since, self.last_off = None, off
            if self.is_away:
                self.is_away = False
                self.state = "glance" if off else "focused"
                return "back"
        if not off:
            self.state = "focused"
            self.off_since = None
            if self.nagging:
                self.nagging = False
                return "refocus"
            return None
        if self.off_since is None:
            self.off_since = now
        self.state = "distracted" if now - self.off_since >= self.glance else "glance"
        if now - self.off_since >= self.grace and now - self.last_nag >= self.repeat:
            self.last_nag, self.nagging = now, True
            return "nag"
        return None


def track_length(path):
    """Seconds, via ffprobe (pygame can't tell a music stream's length); None if unknown."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                              str(path)], capture_output=True, text=True, timeout=15).stdout
        return float(out.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def seek_target(arg, pos):
    """'+30' / '-30' relative to pos; '95' or '1:35' or '1:02:03' absolute. Seconds."""
    if arg[:1] in "+-":
        return pos + float(arg)
    total = 0.0
    for part in arg.split(":"):
        total = total * 60 + float(part)
    return total


class Player:
    """Shuffled, looping playlist of one folder: skip, seek, pause, volume duck.
    Two separate pauses: `paused` while you're away (automatic), `user_paused` from the pause button."""

    def __init__(self, folder, volume):
        import pygame
        self.err, self.mm = pygame.error, pygame.mixer.music
        self.files = [p for p in sorted(folder.glob("*")) if p.suffix.lower() in MUSIC_EXT]
        random.shuffle(self.files)
        self.i, self.volume, self.level = 0, volume, 1.0
        self.paused = self.user_paused = False
        self.current = self.length = None  # playing track's name and seconds
        self.offset = 0.0  # where the current play() started: get_pos() only counts from there
        if not self.files:
            print(f"! no music in {folder} — running silent")

    @property
    def halted(self):
        return self.paused or self.user_paused

    def tick(self):
        """Call every frame: starts the next track when the current one ends."""
        while self.files and not self.halted and not self.mm.get_busy():
            self._next()

    def _next(self):
        f = self.files[self.i % len(self.files)]
        self.i += 1
        try:
            self.mm.load(f)
            self.mm.set_volume(self.volume * self.level)
            self.current, self.length = f.stem, track_length(f)
            self._play(0.0)
            print(f"\n♪ {f.stem}", flush=True)
        except self.err as e:
            print(f"\n! skipping {f.name}: {e}")
            self.files.remove(f)

    def _play(self, start):
        self.mm.play(start=start)
        self.offset = start
        if self.halted:
            self.mm.pause()  # skipping or seeking while paused stays paused

    def position(self):
        p = self.mm.get_pos()  # ms since play(), stops while paused, -1 when idle
        return self.offset + (p / 1000 if p > 0 else 0.0) if self.current else 0.0

    def seek(self, seconds):
        if self.current:
            top = (self.length or seconds + 1) - 1
            self._play(max(0.0, min(seconds, top)))

    def skip(self, step):
        """step=1 next track, -1 previous."""
        if self.files:
            self.i += step - 1  # i already points past the current track
            self._next()

    def toggle(self):
        """The pause button: music only; the session keeps tracking."""
        self.user_paused = not self.user_paused
        if self.user_paused:
            self.mm.pause()
        elif not self.paused:
            self.mm.unpause()

    def pause(self):
        self.paused = True
        self.mm.pause()

    def resume(self):
        self.paused = False
        if not self.user_paused:
            self.mm.unpause()

    def duck(self, on):
        self.level = 0.3 if on else 1.0
        self.mm.set_volume(self.volume * self.level)


def load_sounds(cfg):
    """One nag sound per mode from config (`sound`, `sound_volume`); relative paths are from here."""
    import pygame
    sounds = {}
    for mode, m in cfg["modes"].items():
        try:
            sounds[mode] = pygame.mixer.Sound(HERE / m["sound"])
        except (pygame.error, FileNotFoundError) as e:
            sys.exit(f"[modes.{mode}] sound {m['sound']!r}: {e}")
        sounds[mode].set_volume(m.get("sound_volume", 0.7))
    return sounds


def notify(urgency, text):
    try:
        subprocess.Popen(["notify-send", "-u", urgency, "Focus", text], stderr=subprocess.DEVNULL)
    except OSError:
        pass


def telegram(tg, text):
    token, chat = tg.get("token") or os.environ.get("FOCUS_TG_TOKEN"), tg.get("chat_id")
    if not token or not chat:
        return

    def send():
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=10)
        except Exception as e:
            print(f"\n! telegram failed: {e}")

    threading.Thread(target=send, daemon=True).start()


def nag(mode, sounds, player, tg):
    sounds[mode].play()
    if mode == "soft":
        player.duck(True)
    elif mode == "focus":
        notify("normal", "Eyes up. Back to work.")
    else:
        notify("critical", "EYES UP. FOCUS.")
        telegram(tg, "👁 You looked away. Back to work. Now.")


def tally(state, dt, pending, *buckets):
    """Add dt seconds to `state` in each bucket; returns the glance time still pending.
    A glance's time waits until we know what it was: distracted if it grew past glance_seconds,
    focused if you looked back (or left) in time."""
    if state == "glance":
        return pending + dt
    for b in buckets:
        if pending:
            b["distracted" if state == "distracted" else "focused"] += pending
        b[state] += dt
    return 0.0


XP_RATE = {"soft": 1, "focus": 2, "hard": 3}  # same as the learn_ai app (Focus.kt): XP per 5 focused minutes
STATUS_FILE = ".status"  # in HERE; `focus watch` shows it


def seg_xp(seg):
    return int(seg["focused"] * XP_RATE.get(seg["mode"], 1) // 300)


def looking_away(dp, dy, de, cfg, paper=False):
    """Off the work screen? Paper on: wider up/down and eye margins (writing on the desk); yaw unchanged."""
    pitch_tol, eye_tol = ((cfg.get("paper_pitch_tolerance", 30), cfg.get("paper_eye_tolerance", 0.8)) if paper
                          else (cfg["pitch_tolerance"], cfg["eye_tolerance"]))
    return abs(dp) > pitch_tol or abs(dy) > cfg["yaw_tolerance"] or de > eye_tol


def status_text(mode, state, stats, pending, duration, xp, nags, track, angles):
    """The live view `focus watch` shows, rewritten every second."""
    tracked = sum(stats.values()) + pending
    share = stats["focused"] / tracked if tracked else 1.0
    bar = "█" * round(share * 20) + "░" * (20 - round(share * 20))
    shown = {"focused": "✅ FOCUSED", "glance": "👀 glance", "distracted": "❌ DISTRACTED", "away": "💤 away"}[state]
    return (f"{mode} mode · session {fmt(duration)} · ♪ {track or '—'}\n\n"
            f"  {shown}        {angles}\n\n"
            f"  Focused     {fmt(stats['focused']):>8}   {bar} {share:.0%}\n"
            f"  Distracted  {fmt(stats['distracted']):>8}\n"
            f"  Away        {fmt(stats['away']):>8}\n\n"
            f"  Nags {nags} · XP this session {xp}\n")


def write_status(text, data):
    """.status (text, `focus watch`) and .status.json (the desktop app, app.py). Atomic: never half a file."""
    for name, body in ((STATUS_FILE, text), (STATUS_FILE + ".json", json.dumps(data))):
        tmp = HERE / (name + ".tmp")
        tmp.write_text(body)
        tmp.replace(HERE / name)


def new_segment(mode):
    """Stats of one stretch in one mode; a mode switch or the end of the session closes it."""
    return {"start": datetime.now().astimezone().isoformat(timespec="seconds"), "mode": mode,
            "focused": 0.0, "distracted": 0.0, "away": 0.0, "nags": 0}


def save_segment(seg):
    """Append a closed segment as one JSON line to sessions.jsonl: the source for stats and charts."""
    if seg["focused"] + seg["distracted"] + seg["away"] < 30:
        return  # test runs and instant mode flips aren't sessions
    row = {**seg, "end": datetime.now().astimezone().isoformat(timespec="seconds")}
    row.update({k: round(seg[k]) for k in ("focused", "distracted", "away")})
    with (HERE / "sessions.jsonl").open("a") as f:
        f.write(json.dumps(row) + "\n")


def upload_sessions(sync):
    """PUT the whole sessions.jsonl (as a JSON list) to the learn_ai app's /focus. Whole list each time, so a
    failed upload heals at the next session. Non-daemon thread: a session's last upload finishes before exit."""
    if not sync.get("url"):
        return
    key_file = Path(sync["key_file"]).expanduser()
    key = next((ln.split("=", 1)[1].strip() for ln in key_file.read_text().splitlines()
                if ln.startswith("learnai.key=")), "") if key_file.exists() else ""
    if not key:
        print(f"\n! sync: no learnai.key= line in {key_file}")
        return
    path = HERE / "sessions.jsonl"
    rows = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()] if path.exists() else []

    def send():
        req = urllib.request.Request(sync["url"], data=json.dumps(rows).encode(), method="PUT", headers={
            "X-Learnai-Key": key, "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (focus guard)"})  # Cloudflare's bot filter 403s Python's default agent
        try:
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"\n! sync failed: {e}")

    threading.Thread(target=send).start()


def wrap(deg):
    return (deg + 180) % 360 - 180


def fmt(sec):
    return f"{int(sec // 60)}m{int(sec % 60):02d}s"


def main():
    ap = argparse.ArgumentParser(description="Webcam focus guard")
    ap.add_argument("mode", choices=["soft", "focus", "hard"])
    ap.add_argument("--preview", action="store_true", help="show camera window with head angles")
    args = ap.parse_args()
    cfg = tomllib.loads((HERE / "config.toml").read_text())
    m = cfg["modes"][args.mode]
    MODE_FILE.write_text(args.mode)
    CMD_FILE.unlink(missing_ok=True)  # commands queued for an earlier session must not fire in this one
    # `focus stop` sends SIGTERM; exit through the finally block so the summary is logged
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    # `focus next|prev` send USR1/USR2; `focus <mode>|seek|pause` write CMD_FILE + HUP. Queued, run in the loop
    cmds = []
    for sig, name in ((signal.SIGUSR1, "next"), (signal.SIGUSR2, "prev"), (signal.SIGHUP, "hup")):
        signal.signal(sig, lambda *_, n=name: cmds.append(n))

    import cv2
    import mediapipe as mp
    import numpy as np
    import pygame
    from mediapipe.tasks.python import BaseOptions, vision

    cap = cv2.VideoCapture(cfg["camera"])
    if not cap.isOpened():
        sys.exit(f"cannot open camera {cfg['camera']} (change `camera` in config.toml)")
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not MODEL.exists():
        print("downloading face model (~4 MB)...")
        MODEL.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(MODEL_URL, MODEL)
    lm = vision.FaceLandmarker.create_from_options(vision.FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(MODEL)),
        running_mode=vision.RunningMode.VIDEO, num_faces=1,
        output_facial_transformation_matrixes=True, output_face_blendshapes=True,
        min_face_detection_confidence=0.3, min_face_presence_confidence=0.3,
        min_tracking_confidence=0.3))

    pygame.mixer.init(44100, -16, 2)
    sounds = load_sounds(cfg)
    player = Player(HERE / "music" / args.mode, m["volume"])

    def read():
        ok, frame = cap.read()
        if not ok:
            sys.exit("camera stopped delivering frames")
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        res = lm.detect_for_video(img, int(time.monotonic() * 1000))
        if not res.facial_transformation_matrixes:
            return frame, None
        ang = cv2.RQDecomp3x3(np.asarray(res.facial_transformation_matrixes[0])[:3, :3])[0]
        # eyes often drop to the phone while the head barely moves: eyeLookDown is 0..1
        bs = {c.category_name: c.score for c in res.face_blendshapes[0]}
        eyes = (bs["eyeLookDownLeft"] + bs["eyeLookDownRight"]) / 2
        return frame, (ang[0], ang[1], eyes)  # pitch, yaw in degrees; eyes 0..1

    tracker = Tracker(m["grace"], m["repeat"], cfg["away_seconds"], cfg["lost_seconds"],
                                      cfg.get("glance_seconds", 3))
    stats = {"focused": 0.0, "distracted": 0.0, "away": 0.0}  # whole session, for the summary line
    seg = None  # current mode's segment, saved to sessions.jsonl
    try:
        write_status(f"{args.mode} mode · calibrating: look at your work screen\n",
                     {"state": "calibrating", "mode": args.mode})
        print(f"Calibrating: look at your WORK screen for {cfg['calibration_seconds']}s...")
        notify("normal", f"Calibrating — look at your work screen for {cfg['calibration_seconds']}s")
        samples, end, warned = [], time.monotonic() + cfg["calibration_seconds"], False
        while time.monotonic() < end or len(samples) < 5:
            pose = read()[1]
            if pose:
                samples.append(pose)
            elif time.monotonic() > end and not warned:
                print("  no face yet — sit in front of the camera")
                warned = True
        base_p = statistics.median(s[0] for s in samples)
        base_y = statistics.median(s[1] for s in samples)
        base_e = statistics.median(s[2] for s in samples)
        print(f"Calibrated. Mode: {args.mode}. Ctrl+C to stop.\n")
        notify("normal", f"{args.mode} mode on")
        tty = sys.stdout.isatty()

        seg = new_segment(args.mode)
        pending = 0.0  # seconds of the current glance, not yet counted
        paper = False  # `focus paper`: wider up/down + eye margin, for writing on paper while reading the screen
        last = t0 = next_status = time.monotonic()
        closed_xp = closed_nags = 0  # from this session's earlier mode segments
        while True:
            time.sleep(max(0.0, last + 0.1 - time.monotonic()))  # ~10 fps is plenty, saves CPU
            frame, pose = read()
            now = time.monotonic()
            while cmds:
                c = cmds.pop(0)
                if c == "hup":  # run the queued command lines; a bare SIGHUP (older `focus`) means switch
                    cmds[:0] = take_cmds() or ["switch"]
                    continue
                verb, _, arg = c.partition(" ")
                if verb in ("next", "prev"):
                    player.skip(1 if verb == "next" else -1)
                    continue
                if verb == "seek":
                    try:
                        player.seek(seek_target(arg, player.position()))
                    except ValueError:
                        print(f"\n! bad seek {arg!r}", flush=True)
                    continue
                if verb == "pause":
                    player.toggle()
                    continue
                if verb == "paper":
                    paper = not paper
                    print(f"\n→ paper {'on' if paper else 'off'}", flush=True)
                    continue
                if verb != "switch":
                    print(f"\n! unknown command {c!r}", flush=True)
                    continue
                cfg = tomllib.loads((HERE / "config.toml").read_text())  # picks up config edits too
                sounds = load_sounds(cfg)
                new = MODE_FILE.read_text().strip()
                if new in cfg["modes"] and new != args.mode:
                    save_segment(seg)
                    upload_sessions(cfg.get("sync", {}))
                    closed_xp, closed_nags = closed_xp + seg_xp(seg), closed_nags + seg["nags"]
                    seg = new_segment(new)
                    args.mode, m = new, cfg["modes"][new]
                    player.mm.stop()
                    player = Player(HERE / "music" / new, m["volume"])
                    tracker = Tracker(m["grace"], m["repeat"], cfg["away_seconds"], cfg["lost_seconds"],
                                      cfg.get("glance_seconds", 3))
                    print(f"\n→ {new} mode", flush=True)
                    notify("normal", f"{new} mode on")
            dp = dy = de = 0.0
            if pose:
                dp, dy, de = wrap(pose[0] - base_p), wrap(pose[1] - base_y), pose[2] - base_e
                off = looking_away(dp, dy, de, cfg, paper)
            else:
                off = None
            ev = tracker.update(now, off)
            if ev == "away":
                player.pause()
            elif ev == "back":
                player.duck(False)
                player.resume()
            elif ev == "nag":
                nag(args.mode, sounds, player, cfg.get("telegram", {}))
                seg["nags"] += 1
            elif ev == "refocus":
                player.duck(False)
            player.tick()

            state = tracker.state
            pending = tally(state, now - last, pending, stats, seg)
            last = now
            if now >= next_status:
                next_status = now + 1
                live = {"mode": args.mode, "state": state, **stats, "pending": pending, "duration": now - t0,
                        "xp": closed_xp + seg_xp(seg), "nags": closed_nags + seg["nags"], "track": player.current,
                        "pos": player.position(), "len": player.length, "music_paused": player.user_paused,
                        "paper": paper, "angles": (f"pitch {dp:+.0f}  eyes {de:+.2f}" if pose else "no face")
                        + ("  ·  paper margin on" if paper else "")}
                write_status(status_text(live["mode"], state, stats, pending, live["duration"], live["xp"],
                                         live["nags"], live["track"], live["angles"]), live)
            face = "" if pose else " (no face)"
            if tty:
                print(f"\r[{args.mode}] {state:<10} pitch {dp:+6.1f} yaw {dy:+6.1f} eyes {de:+.2f}{face:<10} | "
                      f"focused {fmt(stats['focused'])} distracted {fmt(stats['distracted'])}   ",
                      end="", flush=True)
            if args.preview:
                cv2.putText(frame, f"{state}{face} pitch {dp:+.0f} yaw {dy:+.0f} eyes {de:+.2f}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7,
                            {"focused": (0, 255, 0), "glance": (0, 200, 255)}.get(state, (0, 0, 255)), 2)
                cv2.imshow("focus", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    except KeyboardInterrupt:
        pass
    finally:
        if seg:
            save_segment(seg)
            upload_sessions(cfg.get("sync", {}))
        total = sum(stats.values()) or 1
        summary = (f"Session: focused {fmt(stats['focused'])} ({stats['focused'] / total:.0%}), "
                   f"distracted {fmt(stats['distracted'])}, away {fmt(stats['away'])}")
        print(f"\n\n{summary}")
        write_status(f"not running\n\nLast {summary[0].lower()}{summary[1:]}\n", {"state": "stopped", "summary": summary})
        cap.release()
        lm.close()  # explicit, else mediapipe's __del__ throws at interpreter shutdown
        pygame.mixer.quit()


if __name__ == "__main__":
    main()
