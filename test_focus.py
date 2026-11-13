# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["mediapipe>=0.10.14", "pygame>=2.5"]
# ///
"""Self-check for the focus state machine: uv run test_focus.py"""
from focus import Tracker, looking_away, seek_target, tally

assert seek_target("+30", 100) == 130 and seek_target("-30", 100) == 70
assert seek_target("95", 0) == 95 and seek_target("1:35", 0) == 95 and seek_target("1:02:03", 0) == 3723


def events(t, seq):
    return [(n, e) for n, o in seq if (e := t.update(n, o))]


# paper margin: writing on the desk (head +20, eyes +0.6) is fine with paper on; turning the head still isn't
cfg = {"pitch_tolerance": 8, "yaw_tolerance": 25, "eye_tolerance": 0.25,
       "paper_pitch_tolerance": 30, "paper_eye_tolerance": 0.8}
assert looking_away(11, 0, 0.44, cfg) and not looking_away(-1, 0, -0.01, cfg)
assert looking_away(20, 0, 0.6, cfg) and not looking_away(20, 0, 0.6, cfg, paper=True)
assert looking_away(35, 0, 0.6, cfg, paper=True) and looking_away(0, 40, 0, cfg, paper=True)

# on screen -> nag after grace, repeat, refocus; face lost from on-screen -> away after `away`
t = Tracker(grace=3, repeat=5, away=2, hold=10, glance=1)
assert events(t, [(0, False), (1, True), (3, True), (4, True), (6, True), (9, True),
                  (10, False), (11, None), (12, None), (13, None), (14, False), (15, True)]) == \
    [(4, "nag"), (9, "nag"), (10, "refocus"), (13, "away"), (14, "back")]

# brief face loss while distracted doesn't reset the grace timer
t = Tracker(grace=3, repeat=5, away=2, hold=10, glance=1)
assert [t.update(n, o) for n, o in [(0, True), (1, None), (2, True), (3, True)]] == [None, None, None, "nag"]

# head drops out of view while looking down: keeps nagging, only away after `hold`
t = Tracker(grace=3, repeat=5, away=2, hold=10, glance=1)
assert events(t, [(0, True), (1, None), (3, None), (5, None), (8, None), (11, None), (12, False)]) == \
    [(3, "nag"), (8, "nag"), (11, "away"), (12, "back")]
assert t.state == "focused"

# under `glance` s looking away is a glance (typing), then distracted
t = Tracker(grace=5, repeat=5, away=2, hold=10, glance=3)
states = []
for n, o in [(0, True), (2.9, True), (3, True), (4, False), (5, True)]:
    t.update(n, o)
    states.append(t.state)
assert states == ["glance", "glance", "distracted", "focused", "glance"], states

# glance time counts as focused if it ends in time, as distracted if it grows into a distraction
b = {"focused": 0.0, "distracted": 0.0, "away": 0.0}
p = 0.0
for state in ["focused", "glance", "glance", "focused", "glance", "glance", "glance", "distracted", "away"]:
    p = tally(state, 1.0, p, b)
assert b == {"focused": 4.0, "distracted": 4.0, "away": 1.0}, b

# desktop app stats: same cases as the phone app's FocusStatsTest, so both show the same numbers
from datetime import date  # noqa: E402

from app import daily, in_period, level_of, totals  # noqa: E402


def row(day, focused_min, mode="soft", distracted_min=0, nags=0, away_min=0):
    return {"start": f"{day}T10:00:00+01:00", "end": f"{day}T12:00:00+01:00", "mode": mode, "focused": focused_min * 60,
            "distracted": distracted_min * 60, "away": away_min * 60, "nags": nags}


streak, best, today_min, days = daily([
    row("2026-11-03", 70), row("2026-11-05", 61), row("2026-11-06", 30), row("2026-11-06", 40, "hard"),
    row("2026-11-07", 90, "focus"), row("2026-11-08", 60, distracted_min=20, nags=3), row("2026-11-09", 20),
], date(2026, 11, 9))
assert (streak, best, today_min, len(days)) == (4, 4, 20, 14)
assert days[10] == (date(2026, 11, 6), 70)
t = totals([row("2026-11-01", 60, distracted_min=20, away_min=10, nags=2), row("2026-11-02", 40, "hard"),
            row("2026-11-03", 90, "focus")])
assert t == {"xp": 72, "sessions": 3, "duration": 6 * 3600, "focused": 190 * 60, "distracted": 20 * 60,
             "away": 10 * 60, "untracked": (360 - 190 - 20 - 10) * 60, "nags": 2}, t
assert level_of(0) == (1, 0, 100) and level_of(100)[0] == 2 and level_of(541) == (3, 300, 600)
assert daily([row("2026-11-09", 60)], date(2026, 11, 9))[0] == 1  # goal reached today: counts today

# periods, for Tuesday 10 Nov 2026: the week starts on Monday the 9th
rows = [row(d, 10) for d in ("2026-10-31", "2026-11-01", "2026-11-08", "2026-11-09", "2026-11-10")]
days_in = lambda p: [r["start"][:10] for r in in_period(rows, p, date(2026, 11, 10))]  # noqa: E731
assert days_in("today") == ["2026-11-10"]
assert days_in("week") == ["2026-11-09", "2026-11-10"]  # Sunday the 8th is last week
assert days_in("month") == ["2026-11-01", "2026-11-08", "2026-11-09", "2026-11-10"]
assert len(days_in("all")) == 5

# week / month levels: XP of the period only (10 soft minutes a row = 2 XP; the week has 2 rows, the month 4)
from app import xp_of  # noqa: E402
assert [xp_of(in_period(rows, p, date(2026, 11, 10))) for p in ("week", "month")] == [4, 8]
from app import period_level  # noqa: E402
assert period_level(0, "week") == (1, 0, 20) and period_level(98, "week") == (5, 80, 100)
assert period_level(98, "month") == (2, 60, 120)
print("ok")
