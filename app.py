"""Focus desktop app: start / stop / switch mode, skip tracks, the live session, and history stats
(same rules as the learn_ai app's Focus tab, Focus.kt). It drives the `focus` command and reads the files
focus.py writes: .status.json (every second while running) and sessions.jsonl (one row per mode stretch).
Closing the window leaves a running session running. Follows the GNOME light/dark setting (FOCUS_THEME=light|dark
overrides it).

Run: focus app, or "Focus" in the app menu (/usr/bin/python3 app.py: stdlib only; uv's managed Python can't
find Tcl inside a venv, so not `uv run`).
"""
import json
import os
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import ttk

HERE = Path(__file__).resolve().parent
BIN = Path.home() / ".local" / "bin"
PATTERN = r"bin/python[0-9.]* focus\.py"  # the running engine (same as the focus command's)

# Same rules as learn_ai's Focus.kt
DAILY_GOAL_MIN = 60   # focused minutes a day that keep the streak alive
XP_MINUTES = 5        # XP is paid per this many focused minutes...
RATE = {"soft": 1, "focus": 2, "hard": 3}  # ...this much per mode


def xp_of(rows):
    return sum(r["focused"] * RATE.get(r["mode"], 1) for r in rows) // (XP_MINUTES * 60)


def level_of(xp):
    """(level, xp where it starts, xp where the next starts): level L spans 50·L·(L-1) .. 50·L·(L+1)."""
    lv = 1
    while xp >= 50 * lv * (lv + 1):
        lv += 1
    return lv, 50 * lv * (lv - 1), 50 * lv * (lv + 1)


LEVEL_STEP = {"week": 20, "month": 60}  # XP per level: ~50 min in focus mode a week level, ~a good day a month level


def period_level(xp, period):
    """(level, xp where it starts, xp where the next starts) for the week / month levels: one level every step XP."""
    step = LEVEL_STEP[period]
    return xp // step + 1, xp // step * step, (xp // step + 1) * step


def day_of(r):
    return datetime.fromisoformat(r["start"]).date()


def daily(rows, today):
    """(streak, best streak, today's focused minutes, last 14 days [(date, minutes)] oldest first).
    The streak counts goal days up to today; today is still open, so a missing goal today counts from yesterday."""
    secs = {}
    for r in rows:
        secs[day_of(r)] = secs.get(day_of(r), 0) + r["focused"]
    per = {d: s // 60 for d, s in secs.items()}
    goal = {d for d, m in per.items() if m >= DAILY_GOAL_MIN}
    d, streak = (today if today in goal else today - timedelta(1)), 0
    while d in goal:
        streak, d = streak + 1, d - timedelta(1)
    best = run = 0
    prev = None
    for g in sorted(goal):
        run = run + 1 if prev and g == prev + timedelta(1) else 1
        best, prev = max(best, run), g
    days = [(today - timedelta(i), per.get(today - timedelta(i), 0)) for i in range(13, -1, -1)]
    return streak, best, per.get(today, 0), days


def totals(rows):
    """Seconds over a set of rows. untracked = wall clock that is none of focused/distracted/away."""
    dur = sum(max(0, int((datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds()))
              for r in rows)
    f, d, a = (sum(r[k] for r in rows) for k in ("focused", "distracted", "away"))
    return {"xp": xp_of(rows), "sessions": len(rows), "duration": max(dur, f + d + a), "focused": f,
            "distracted": d, "away": a, "untracked": max(0, dur - f - d - a), "nags": sum(r.get("nags", 0) for r in rows)}


PERIODS = {"today": "Today", "week": "This week", "month": "This month", "all": "All time"}


def in_period(rows, period, today):
    """Rows of one period by start day: today, this week (from Monday), this month, or all."""
    first = {"today": today, "week": today - timedelta(today.weekday()), "month": today.replace(day=1)}.get(period)
    return rows if first is None else [r for r in rows if first <= day_of(r) <= today]


def hm(sec):
    m = int(sec) // 60
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m {int(sec) % 60:02d}s" if m < 10 else f"{m}m"


def clock(sec):
    h, rem = divmod(int(sec), 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}"


def load_sessions():
    path = HERE / "sessions.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --- look -----------------------------------------------------------------------------------------------------------

THEMES = {
    "dark": {"bg": "#0f1013", "card": "#18191d", "raised": "#24262c", "line": "#30333a", "text": "#eceef2",
             "muted": "#8b909b", "accent": "#8b9dff", "accent_hover": "#a3b2ff", "on_accent": "#0e1122",
             "focused": "#4ade80", "glance": "#fbbf24", "distracted": "#f87171", "away": "#6b7280",
             "calibrating": "#8b9dff", "stopped": "#6b7280"},
    "light": {"bg": "#f2f3f5", "card": "#ffffff", "raised": "#eceef1", "line": "#dfe2e7", "text": "#15171c",
              "muted": "#6b7280", "accent": "#4f5fe0", "accent_hover": "#3f4fd0", "on_accent": "#ffffff",
              "focused": "#16a34a", "glance": "#d97706", "distracted": "#dc2626", "away": "#9ca3af",
              "calibrating": "#4f5fe0", "stopped": "#9ca3af"},
}
STATE_TEXT = {"focused": "Focused", "glance": "Glance", "distracted": "Distracted", "away": "Away",
              "calibrating": "Calibrating", "stopped": "Not running"}


def system_theme():
    if os.environ.get("FOCUS_THEME") in THEMES:
        return os.environ["FOCUS_THEME"]
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    return "dark" if "dark" in out else "light"


def blend(a, b, k):
    """Colour a mixed with b, k = share of b."""
    x, y = int(a[1:], 16), int(b[1:], 16)
    return "#" + "".join(f"{round(((x >> s) & 255) * (1 - k) + ((y >> s) & 255) * k):02x}" for s in (16, 8, 0))


def round_rect(c, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r,
           x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


class Card(tk.Canvas):
    """A rounded panel; put widgets in .body. expand=True: fills the space it's packed in (else fits its content)."""

    def __init__(self, parent, t, expand=False, pad=22, radius=18):
        super().__init__(parent, bg=t["bg"], highlightthickness=0, bd=0)
        self.t, self.pad, self.radius, self.expand = t, pad, radius, expand
        self.body = tk.Frame(self, bg=t["card"])
        self.win = self.create_window(pad, pad, window=self.body, anchor="nw")
        if not expand:
            self.body.bind("<Configure>", lambda e: self.configure(height=e.height + 2 * pad))
        self.bind("<Configure>", self._draw)

    def _draw(self, e):
        self.delete("shape")
        round_rect(self, 0, 0, e.width, e.height, self.radius, fill=self.t["card"], outline="", tags="shape")
        self.tag_lower("shape")
        self.itemconfigure(self.win, width=e.width - 2 * self.pad,
                           **({"height": e.height - 2 * self.pad} if self.expand else {}))


class Pill(tk.Canvas):
    """A rounded button. kind: primary | ghost | stop | selected."""

    def __init__(self, parent, app, text, command, kind="ghost", font="label", padx=16, pady=8):
        super().__init__(parent, bg=parent["bg"], highlightthickness=0, bd=0)
        self.app, self.command, self.font, self.padx, self.pady = app, command, app.f[font], padx, pady
        self.text, self.kind, self.enabled, self.hover = text, kind, True, False
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-1>", lambda e: self.enabled and self.command())
        self.draw()

    def _hover(self, on):
        self.hover = on
        self.draw()

    def set(self, text=None, kind=None, enabled=None):
        new = (text or self.text, kind or self.kind, self.enabled if enabled is None else enabled)
        if new != (self.text, self.kind, self.enabled):
            self.text, self.kind, self.enabled = new
            self.draw()

    def draw(self):
        t = self.app.t
        w = self.font.measure(self.text) + 2 * self.padx
        h = self.font.metrics("linespace") + 2 * self.pady
        self.configure(width=w, height=h, cursor="hand2" if self.enabled else "arrow")
        if not self.enabled:
            fill, fg = t["raised"], blend(t["muted"], t["raised"], .45)
        elif self.kind in ("primary", "selected"):
            fill, fg = t["accent_hover"] if self.hover else t["accent"], t["on_accent"]
        else:
            fill, fg = t["line"] if self.hover else t["raised"], t["distracted"] if self.kind == "stop" else t["text"]
        self.delete("all")
        round_rect(self, 0, 0, w, h, h / 2, fill=fill, outline="")
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self.font)


class Bar(tk.Canvas):
    """A thin rounded bar of coloured parts [(fraction, colour)], left to right, on a track."""

    def __init__(self, parent, app, height=8):
        super().__init__(parent, height=height, bg=parent["bg"], highlightthickness=0, bd=0)
        self.app, self.parts = app, []
        self.bind("<Configure>", lambda e: self.draw())

    def set(self, parts):
        if parts != self.parts:
            self.parts = parts
            self.draw()

    def draw(self):
        self.delete("all")
        w, h = self.winfo_width(), int(self["height"])
        if w < 2 * h:
            return
        r = h / 2
        self.create_line(r, r, w - r, r, width=h, capstyle="round", fill=self.app.t["raised"])
        ends, acc = [], 0.0
        for frac, colour in self.parts:
            acc = min(1.0, acc + frac)
            ends.append((acc, colour))
        for end, colour in reversed(ends):  # longest first: each part's round cap tucks under the next
            if end > 0:
                self.create_line(r, r, r + (w - 2 * r) * end, r, width=h, capstyle="round", fill=colour)


def mmss(sec):
    sec = int(sec or 0)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class Timeline(tk.Canvas):
    """The track's position with a knob. Click or drag, release to seek: on_seek(seconds).
    After a seek it shows the target for a moment, until the engine's status (1/s) catches up."""

    def __init__(self, parent, app, on_seek, on_move):
        super().__init__(parent, height=18, bg=parent["bg"], highlightthickness=0, bd=0)
        self.app, self.on_seek, self.on_move = app, on_seek, on_move
        self.pos = self.length = 0.0
        self.enabled, self.drag, self.hold_until = False, None, 0.0
        self.bind("<Configure>", lambda e: self.draw())
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _seconds(self, x):
        w = self.winfo_width()
        return max(0.0, min(1.0, (x - 7) / max(1, w - 14))) * self.length

    def _press(self, e):
        if self.enabled and self.length:
            self.drag = self._seconds(e.x)
            self.draw()

    def _release(self, e):
        if self.drag is not None:
            self.jump(self.drag)
            self.on_seek(self.pos)
            self.drag = None

    def jump(self, seconds):
        """Show `seconds` now (a seek was sent) and ignore the engine's older position for 2 s."""
        import time
        self.pos, self.hold_until = max(0.0, min(seconds, self.length or seconds)), time.monotonic() + 2
        self.draw()

    def set(self, pos, length, enabled):
        import time
        self.length, self.enabled = length or 0.0, enabled
        if time.monotonic() > self.hold_until:
            self.pos = pos or 0.0
        self.configure(cursor="hand2" if enabled else "arrow")
        if self.drag is None:
            self.draw()

    def draw(self):
        t, w = self.app.t, self.winfo_width()
        shown = self.drag if self.drag is not None else self.pos
        self.on_move(shown, self.length)
        self.delete("all")
        if w < 20:
            return
        y, x0, x1 = 9, 7, w - 7
        self.create_line(x0, y, x1, y, width=4, capstyle="round", fill=t["raised"])
        if self.enabled and self.length:
            x = x0 + (x1 - x0) * min(1.0, shown / self.length)
            self.create_line(x0, y, x, y, width=4, capstyle="round", fill=t["accent"])
            self.create_oval(x - 6, y - 6, x + 6, y + 6, fill=t["text"], outline="")


# --- window ---------------------------------------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        t = self.t = THEMES[system_theme()]
        family = "Ubuntu Sans" if "Ubuntu Sans" in tkfont.families(root) else "TkDefaultFont"
        font = lambda size, weight="normal": tkfont.Font(root, family=family, size=size, weight=weight)  # noqa: E731
        self.f = {"small": font(9), "label": font(10), "body": font(11), "value": font(15, "bold"),
                  "title": font(13, "bold"), "h1": font(20, "bold"), "timer": font(30, "bold")}
        root.title("Focus")
        root.configure(bg=t["bg"])
        root.geometry("1160x840")
        root.minsize(1040, 780)
        self.sessions_mtime, self.sessions, self.days = None, [], []
        self.running = False
        self.live_mode, self.live_state = None, "stopped"
        self.has_track = self.music_paused = self.paper_on = False
        self.sel = (HERE / ".mode").read_text().strip() if (HERE / ".mode").exists() else "focus"
        self.busy = False
        self.toast_job = None
        self.style_table()

        # Header
        head = tk.Frame(root, bg=t["bg"])
        head.pack(fill="x", padx=28, pady=(22, 14))
        self.lbl(head, "Focus", "h1").pack(side="left")
        self.status = self.lbl(head, "", "body", "muted")
        self.status.pack(side="left", padx=14, pady=(8, 0))

        cols = tk.Frame(root, bg=t["bg"])
        cols.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        cols.columnconfigure(0, weight=5, uniform="c")
        cols.columnconfigure(1, weight=6, uniform="c")
        cols.rowconfigure(0, weight=1)
        left = tk.Frame(cols, bg=t["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=8)
        right = tk.Frame(cols, bg=t["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=8)

        self.build_session(left)
        self.build_progress(left)
        self.toast = self.lbl(left, "", "label", "muted")
        self.toast.pack(anchor="w", padx=6, pady=(10, 0))
        self.build_history(right)
        self.tick()

    # --- small builders ---------------------------------------------------------------------------------------------
    def lbl(self, parent, text, font="body", colour="text", **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=self.t[colour], font=self.f[font], **kw)

    def tile(self, parent, label, column, sub=False):
        """Small caption over a big number, in a grid column. Returns (value label, sub label or None)."""
        f = tk.Frame(parent, bg=parent["bg"])
        f.grid(row=0, column=column, sticky="w")
        parent.columnconfigure(column, weight=1, uniform="tile")
        self.lbl(f, label, "label", "muted").pack(anchor="w")
        value = self.lbl(f, "–", "value")
        value.pack(anchor="w")
        note = self.lbl(f, "", "small", "muted") if sub else None
        if note:
            note.pack(anchor="w")
        return value, note

    def section(self, parent, text):
        return self.lbl(parent, text.upper(), "small", "muted")

    def style_table(self):
        t, s = self.t, ttk.Style(self.root)
        s.theme_use("clam")
        flat = {"bordercolor": t["card"], "lightcolor": t["card"], "darkcolor": t["card"], "borderwidth": 0}
        s.configure("F.Treeview", background=t["card"], fieldbackground=t["card"], foreground=t["text"],
                    rowheight=32, font=self.f["label"], relief="flat", **flat)
        s.configure("F.Treeview.Heading", background=t["card"], foreground=t["muted"], font=self.f["small"],
                    relief="flat", padding=(4, 6), **flat)
        s.map("F.Treeview.Heading", background=[("active", t["card"])])
        s.map("F.Treeview", background=[("selected", t["raised"])], foreground=[("selected", t["text"])])
        s.layout("F.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

    # --- session card -----------------------------------------------------------------------------------------------
    def build_session(self, parent):
        card = Card(parent, self.t)
        card.pack(fill="x")
        b = card.body
        top = tk.Frame(b, bg=b["bg"])
        top.pack(fill="x")
        self.mode_pills = {}
        for m in ("soft", "focus", "hard"):
            p = Pill(top, self, m.capitalize(), lambda m=m: self.pick(m), font="label", padx=18)
            p.pack(side="left", padx=(0, 6))
            self.mode_pills[m] = p
        self.main = Pill(top, self, "Start", self.toggle, kind="primary", font="body", padx=26, pady=9)
        self.main.pack(side="right")
        # wider up/down + eye margin while writing on paper; `focus paper` does the same
        self.paper = Pill(top, self, "Paper", lambda: self.run("paper"), font="label", padx=16)
        self.paper.pack(side="right", padx=(0, 10))

        row = tk.Frame(b, bg=b["bg"])
        row.pack(fill="x", pady=(26, 0))
        self.dot = tk.Canvas(row, width=14, height=14, bg=b["bg"], highlightthickness=0)
        self.dot.pack(side="left", pady=(10, 0))
        self.state = self.lbl(row, "", "h1")
        self.state.pack(side="left", padx=(10, 0))
        self.timer = self.lbl(row, "0:00:00", "timer")
        self.timer.pack(side="right")
        self.angles = self.lbl(b, "", "label", "muted")
        self.angles.pack(anchor="w", padx=(24, 0))

        self.share = Bar(b, self, height=10)
        self.share.pack(fill="x", pady=(18, 18))
        tiles = tk.Frame(b, bg=b["bg"])
        tiles.pack(fill="x")
        self.live_tiles = {k: self.tile(tiles, k.capitalize() if k != "xp" else "XP", i)[0]
                           for i, k in enumerate(("focused", "distracted", "away", "nags", "xp"))}

        # Music player: track, timeline (click / drag to seek), transport buttons
        tk.Frame(b, bg=self.t["line"], height=1).pack(fill="x", pady=(18, 12))
        play = tk.Frame(b, bg=b["bg"])
        play.pack(fill="x")
        self.lbl(play, "Now playing", "small", "muted").pack(side="left")
        self.track_time = self.lbl(play, "", "small", "muted")
        self.track_time.pack(side="right")
        self.track = self.lbl(b, "–", "label", anchor="w")
        self.track.pack(anchor="w", fill="x", pady=(2, 4))
        self.timeline = Timeline(b, self, on_seek=lambda s: self.run("seek", str(int(s))),
                                 on_move=lambda pos, length: self.track_time.config(
                                     text=f"{mmss(pos)} / {mmss(length)}" if length else ""))
        self.timeline.pack(fill="x")
        keys = tk.Frame(b, bg=b["bg"])
        keys.pack(pady=(8, 0))
        small = {"font": "label", "padx": 14, "pady": 6}
        self.media = {
            "prev": Pill(keys, self, "‹‹  Prev", lambda: self.run("prev"), **small),
            "back": Pill(keys, self, "−30s", lambda: self.nudge(-30), **small),
            "pause": Pill(keys, self, "Pause", lambda: self.run("pause"), font="label", padx=22, pady=6),
            "fwd": Pill(keys, self, "+30s", lambda: self.nudge(30), **small),
            "next": Pill(keys, self, "Next  ››", lambda: self.run("next"), **small),
        }
        for p in self.media.values():
            p.pack(side="left", padx=3)

    # --- progress card ----------------------------------------------------------------------------------------------
    def build_progress(self, parent):
        card = Card(parent, self.t)
        card.pack(fill="x", pady=(16, 0))
        b = card.body
        # one level per week and per month, each starting again from level 1
        levels = tk.Frame(b, bg=b["bg"])
        levels.pack(fill="x")
        levels.columnconfigure((0, 1), weight=1, uniform="lv")
        self.levels = {}
        for i, (key, name) in enumerate((("week", "This week"), ("month", "This month"))):
            col = tk.Frame(levels, bg=b["bg"])
            col.grid(row=0, column=i, sticky="ew", padx=(0, 14) if i == 0 else (14, 0))
            self.lbl(col, name, "small", "muted").pack(anchor="w")
            head = tk.Frame(col, bg=b["bg"])
            head.pack(fill="x")
            level, total = self.lbl(head, "Level 1", "title"), self.lbl(head, "", "label", "muted")
            level.pack(side="left")
            total.pack(side="right", anchor="s", pady=(0, 3))
            bar = Bar(col, self, height=8)
            bar.pack(fill="x", pady=(8, 4))
            nxt = self.lbl(col, "", "small", "muted")
            nxt.pack(anchor="w")
            self.levels[key] = (level, total, bar, nxt)

        tiles = tk.Frame(b, bg=b["bg"])
        tiles.pack(fill="x", pady=(18, 0))
        self.streak, self.best = self.tile(tiles, "Streak", 0, sub=True)
        self.today, self.today_note = self.tile(tiles, "Today", 1, sub=True)
        self.todaybar = Bar(b, self, height=6)
        self.todaybar.pack(fill="x", pady=(10, 0))
        self.lbl(b, f"XP: 1 · 2 · 3 per {XP_MINUTES} focused minutes in soft · focus · hard. A level every "
                    f"{LEVEL_STEP['week']} XP in the week, {LEVEL_STEP['month']} XP in the month, from level 1 again "
                    f"every Monday and every 1st. Streak: days with {DAILY_GOAL_MIN}+ focused minutes.", "small", "muted",
                 justify="left", wraplength=440).pack(anchor="w", pady=(16, 0))

    # --- history card -----------------------------------------------------------------------------------------------
    def build_history(self, parent):
        card = Card(parent, self.t, expand=True)
        card.pack(fill="both", expand=True)
        b = card.body
        top = tk.Frame(b, bg=b["bg"])
        top.pack(fill="x")
        self.lbl(top, "History", "title").pack(side="left")
        self.period = "month"
        self.period_pills = {}
        for key, text in reversed(PERIODS.items()):  # packed from the right: shows Today ... All time
            p = Pill(top, self, text, lambda k=key: self.set_period(k), font="label", padx=12, pady=6)
            p.pack(side="right", padx=(6, 0))
            self.period_pills[key] = p

        tiles = tk.Frame(b, bg=b["bg"])
        tiles.pack(fill="x", pady=(20, 0))
        self.hist_tiles = {k: self.tile(tiles, label, i)[0] for i, (k, label) in enumerate(
            (("sessions", "Sessions"), ("duration", "Time"), ("xp", "XP"), ("on", "On screen"), ("nags", "Nags")))}

        self.breakdown = Bar(b, self, height=10)
        self.breakdown.pack(fill="x", pady=(18, 10))
        legend = tk.Frame(b, bg=b["bg"])
        legend.pack(fill="x")
        self.legend = {}
        colours = {"focused": self.t["focused"], "distracted": self.t["distracted"], "away": self.t["away"],
                   "untracked": self.t["line"]}
        for i, (k, colour) in enumerate(colours.items()):
            f = tk.Frame(legend, bg=b["bg"])
            f.grid(row=0, column=i, sticky="w")
            legend.columnconfigure(i, weight=1, uniform="lg")
            dot = tk.Canvas(f, width=10, height=10, bg=b["bg"], highlightthickness=0)
            dot.create_oval(1, 1, 9, 9, fill=colour, outline="")
            dot.pack(side="left")
            self.legend[k] = self.lbl(f, k, "label", "muted")
            self.legend[k].pack(side="left", padx=(6, 0))

        self.section(b, "Last 14 days").pack(anchor="w", pady=(22, 6))
        self.chart = tk.Canvas(b, height=130, bg=b["bg"], highlightthickness=0)
        self.chart.pack(fill="x")
        self.chart.bind("<Configure>", lambda e: self.draw_chart())

        self.section(b, "Sessions").pack(anchor="w", pady=(18, 4))
        cols = ("when", "mode", "focused", "on screen", "nags", "xp")
        self.table = ttk.Treeview(b, columns=cols, show="headings", style="F.Treeview", selectmode="none")
        for c, w in zip(cols, (150, 70, 80, 80, 60, 50)):
            self.table.heading(c, text=c.upper() if c != "xp" else "XP", anchor="w" if c in ("when", "mode") else "e")
            self.table.column(c, width=w, anchor="w" if c in ("when", "mode") else "e", stretch=c == "when")
        self.table.tag_configure("odd", background=blend(self.t["card"], self.t["raised"], .45))
        self.table.pack(fill="both", expand=True)

    # --- actions ----------------------------------------------------------------------------------------------------
    def pick(self, mode):
        self.sel = mode
        if self.running and mode != self.live_mode:
            self.run(mode)  # running: switch live
        self.paint_controls()

    def toggle(self):
        self.run("stop" if self.running else self.sel)

    def set_period(self, key):
        self.period = key
        self.draw_stats()

    def show(self, text):
        self.toast.config(text=text)
        if self.toast_job:
            self.root.after_cancel(self.toast_job)
        self.toast_job = self.root.after(8000, lambda: self.toast.config(text=""))

    def nudge(self, seconds):
        """±30 s: show the new position at once, then tell the engine."""
        self.timeline.jump(self.timeline.pos + seconds)
        self.run("seek", f"{seconds:+d}")

    def run(self, cmd, *args):
        """Run `focus <cmd> [args]` off the UI thread. Mode/stop commands lock the controls and report in the
        toast; player commands (seek, pause, next, prev) are instant and quiet."""
        player = cmd in ("seek", "pause", "next", "prev", "paper")
        if not player:
            self.busy = True
            self.paint_controls()
            self.show("Stopping..." if cmd == "stop" else f"Switching to {cmd}..." if self.running
                      else f"Starting {cmd} mode: look at your work screen")
        env = {**os.environ, "PATH": f"{BIN}:{os.environ.get('PATH', '')}"}  # menu launchers lack ~/.local/bin

        def work():
            try:
                r = subprocess.run([str(BIN / "focus"), cmd, *args], capture_output=True, text=True, timeout=60,
                                   env=env)
                out = (r.stdout + r.stderr).strip().splitlines()
                text = out[-1] if out else ""
            except (OSError, subprocess.SubprocessError) as e:
                text = f"focus {cmd} failed: {e}"
            self.root.after(0, lambda: None if player else self.done(text))

        threading.Thread(target=work, daemon=True).start()

    def done(self, text):
        self.busy = False
        self.show(text)
        self.paint_controls()

    # --- refresh ----------------------------------------------------------------------------------------------------
    def tick(self):
        self.draw_live()
        path = HERE / "sessions.jsonl"
        mtime = path.stat().st_mtime if path.exists() else None
        if mtime != self.sessions_mtime or date.today() != getattr(self, "drawn_day", None):
            self.sessions_mtime, self.sessions, self.drawn_day = mtime, load_sessions(), date.today()
            self.draw_stats()
        self.root.after(1000, self.tick)

    def paint_controls(self):
        mode = self.live_mode if self.running else self.sel
        for m, p in self.mode_pills.items():
            p.set(kind="selected" if m == mode else "ghost", enabled=not self.busy)
        self.main.set(text="Stop" if self.running else "Start", kind="stop" if self.running else "primary",
                      enabled=not self.busy)
        playing = self.running and not self.busy and self.live_state not in ("calibrating", "stopped") and self.has_track
        for p in self.media.values():
            p.set(enabled=playing)
        self.paper.set(kind="selected" if self.paper_on else "ghost",
                       enabled=self.running and not self.busy and self.live_state not in ("calibrating", "stopped"))
        self.media["pause"].set(text="Play" if self.music_paused else "Pause",
                                kind="selected" if self.music_paused else "ghost")

    def draw_live(self):
        t = self.t
        self.running = subprocess.run(["pgrep", "-f", PATTERN], capture_output=True).returncode == 0
        try:
            s = json.loads((HERE / ".status.json").read_text())
        except (OSError, ValueError):
            s = {}
        if not self.running:
            s = {"state": "stopped", "summary": s.get("summary", "")}
        state = self.live_state = s.get("state", "stopped")
        self.live_mode = s.get("mode")
        self.has_track, self.music_paused = bool(s.get("track")), bool(s.get("music_paused"))
        self.paper_on = bool(s.get("paper"))
        self.timeline.set(s.get("pos"), s.get("len"), enabled=self.running and self.has_track and not self.busy)
        colour = t.get(state, t["text"])
        self.dot.delete("all")
        self.dot.create_oval(1, 1, 13, 13, fill=colour, outline="")
        self.state.config(text=STATE_TEXT.get(state, state), fg=colour if state != "stopped" else t["muted"])
        self.status.config(text={"stopped": "Not running"}.get(
            state, f"{'Calibrating' if state == 'calibrating' else 'Running'} · {self.live_mode} mode"))
        self.paint_controls()

        if "focused" not in s:  # stopped or calibrating: no numbers yet
            self.timer.config(text="0:00:00", fg=t["muted"])
            self.angles.config(text="Look at your work screen for 3 s" if state == "calibrating" else
                               (s.get("summary") or "Pick a mode and press Start").replace("Session:", "Last session:"))
            self.share.set([])
            for v in self.live_tiles.values():
                v.config(text="–")
            self.track.config(text="–")
            return
        tracked = s["focused"] + s["distracted"] + s["away"] + s["pending"]
        self.timer.config(text=clock(s["duration"]), fg=t["text"])
        self.angles.config(text=s["angles"])
        if tracked:
            self.share.set([(s[k] / tracked, t[colour]) for k, colour in (
                ("focused", "focused"), ("pending", "glance"), ("distracted", "distracted"), ("away", "away"))])
        for k in ("focused", "distracted", "away"):
            self.live_tiles[k].config(text=hm(s[k]))
        self.live_tiles["nags"].config(text=str(s["nags"]))
        self.live_tiles["xp"].config(text=f"+{s['xp']}")
        name = s.get("track") or "–"
        self.track.config(text=name if len(name) <= 52 else name[:50] + "…")

    def draw_stats(self):
        t, rows, today = self.t, self.sessions, date.today()
        for key, (level, total, bar, nxt) in self.levels.items():
            xp = xp_of(in_period(rows, key, today))
            lv, start, end = period_level(xp, key)
            level.config(text=f"Level {lv}")
            total.config(text=f"{xp} XP")
            bar.set([((xp - start) / (end - start), t["accent"])])
            nxt.config(text=f"{end - xp} XP to level {lv + 1}")
        streak, best, today_min, self.days = daily(rows, today)
        plural = lambda n: f"{n} day" + ("" if n == 1 else "s")  # noqa: E731
        self.streak.config(text=plural(streak))
        self.best.config(text=f"best {plural(best)}")
        self.today.config(text=hm(today_min * 60))
        self.today_note.config(text=f"of {DAILY_GOAL_MIN}m goal" if today_min < DAILY_GOAL_MIN else "goal reached")
        self.todaybar.set([(min(1.0, today_min / DAILY_GOAL_MIN), t["focused"] if today_min >= DAILY_GOAL_MIN
                            else t["accent"])])
        self.draw_chart()

        for k, p in self.period_pills.items():
            p.set(kind="selected" if k == self.period else "ghost")
        rows = in_period(rows, self.period, today)
        s = totals(rows)
        active = s["focused"] + s["distracted"]
        values = {"sessions": str(s["sessions"]), "duration": hm(s["duration"]), "xp": str(s["xp"]),
                  "on": f"{s['focused'] / active:.0%}" if active else "–", "nags": str(s["nags"])}
        for k, v in values.items():
            self.hist_tiles[k].config(text=v)
        whole = s["duration"] or 1
        self.breakdown.set([(s[k] / whole, c) for k, c in (("focused", t["focused"]), ("distracted", t["distracted"]),
                                                            ("away", t["away"]), ("untracked", t["line"]))])
        for k, label in self.legend.items():
            label.config(text=f"{k} {hm(s[k])}")
        self.table.delete(*self.table.get_children())
        for i, r in enumerate(reversed(rows[-200:])):
            act = r["focused"] + r["distracted"]
            self.table.insert("", "end", tags=("odd",) if i % 2 else (), values=(
                datetime.fromisoformat(r["start"]).strftime("%a %d %b · %H:%M"), r["mode"], hm(r["focused"]),
                f"{r['focused'] * 100 // act}%" if act else "–", r.get("nags", 0), xp_of([r])))

    def draw_chart(self):
        """Focused minutes per day, last 14 days: accent bars reached the goal, the dashed line is the goal."""
        c, t, days = self.chart, self.t, self.days
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if not days or w < 50:
            return
        top_y, base = 16, h - 24
        top = max(DAILY_GOAL_MIN * 1.2, max(m for _, m in days) * 1.12)
        y = lambda m: base - (base - top_y) * m / top  # noqa: E731
        pw = w - 56  # plot width; the margin on the right holds the goal label
        bw = pw / len(days)
        lw = min(bw * .5, 18)
        c.create_line(0, y(DAILY_GOAL_MIN), pw, y(DAILY_GOAL_MIN), fill=t["line"], dash=(3, 4))
        c.create_text(w, y(DAILY_GOAL_MIN), text=f"{DAILY_GOAL_MIN}m goal", anchor="e", fill=t["muted"],
                      font=self.f["small"])
        today = date.today()
        for i, (d, m) in enumerate(days):
            x = i * bw + bw / 2
            if m:
                colour = t["accent"] if m >= DAILY_GOAL_MIN else blend(t["accent"], t["card"], .55)
                c.create_line(x, base - lw / 2, x, min(base - lw / 2, y(m) + lw / 2), width=lw, capstyle="round",
                              fill=colour)
                c.create_text(x, y(m) - 9, text=str(m), fill=t["muted"], font=self.f["small"])
            else:
                c.create_oval(x - 2, base - 4, x + 2, base, fill=t["raised"], outline="")
            c.create_text(x, base + 14, text=str(d.day), font=self.f["small"],
                          fill=t["text"] if d == today else t["muted"])


if __name__ == "__main__":
    root = tk.Tk(className="Focus")  # WM class: matches StartupWMClass in the app-menu launcher
    App(root)
    root.mainloop()
