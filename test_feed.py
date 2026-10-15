"""Self-check for feed.py's plan() and slot math. No network, no files: `python -X utf8 test_feed.py` (from this folder)."""
import datetime as dt
import feed

UTC, H = dt.timezone.utc, dt.timedelta(hours=1)
SCHED = {**feed.DEFAULTS}
TZ = feed.tzinfo("America/New_York")
NOW = dt.datetime(2026, 10, 15, 13, 0, tzinfo=UTC)  # 9:00 in New York
RULES = {"tz": TZ, "room": None, "urgent_hours": 12, "bump_minutes": 30, "promo_min_organic": 5, "promo_min_hours": 48}
ny = lambda y, m, d, hh, mm=0: dt.datetime(y, m, d, hh, mm, tzinfo=TZ).astimezone(UTC)
n = [0]


def item(fmt="carousel", style=None, artist=None, fu=None, nb=None, prio="normal", kind="organic", pref=None, sent=None, **kw):
    n[0] += 1
    return {"id": kw.get("id") or f"p{n[0]:02d}", "format": fmt, "kind": kind, "style": style or f"s{n[0]}", "artists": [artist or f"a{n[0]}"], "priority": prio,
            "fresh_until": fu, "not_before": nb, "added": NOW - dt.timedelta(hours=100 - n[0]), "preferred_date": pref, "sent": sent, "expired": False}


def run(items, sched=(), hours=72, now=NOW, **rules):
    return feed.plan(items, list(sched), feed.slots_between(now, SCHED, hours), now, {**RULES, **rules})


sends = lambda acts: [(a["id"], a["due"]) for a in acts if a["do"] == "send"]
ids = lambda acts: [a["id"] for a in acts if a["do"] == "send"]

# --- slots: every post time in the window, none within 15 minutes, DST-aware
s = feed.slots_between(NOW, SCHED)
assert s[0] == (ny(2026, 10, 15, 12), "carousel") and s[1] == (ny(2026, 10, 15, 16), "reel") and s[2] == (ny(2026, 10, 15, 19), "carousel"), s[:3]
assert len(s) == 9 and all(d <= NOW + 72 * H for d, _ in s)
assert feed.slots_between(ny(2026, 10, 15, 11, 50), SCHED)[0][0] == ny(2026, 10, 15, 16)  # 12:00 is within 15 min: skipped
assert feed.slots_between(ny(2026, 10, 15, 11, 44), SCHED)[0][0] == ny(2026, 10, 15, 12)
fall = feed.slots_between(ny(2026, 10, 31, 0), SCHED)  # clocks go back on Nov 1: noon is 16:00Z before, 17:00Z after
assert [d.strftime("%m-%d %H:%MZ") for d, f in fall if f == "carousel"][:3] == ["10-31 16:00Z", "10-31 23:00Z", "11-01 17:00Z"], fall
spring = feed.slots_between(ny(2027, 3, 13, 0), SCHED)  # clocks go forward on Mar 14 2027: the Reel moves from 21:00Z to 20:00Z
assert [d.strftime("%m-%d %H:%MZ") for d, f in spring if f == "reel"][:2] == ["03-13 21:00Z", "03-14 20:00Z"], spring
ref = feed._NY()
for args, off in [((2026, 10, 31, 12), -4), ((2026, 11, 1, 12), -5), ((2027, 3, 13, 12), -5), ((2027, 3, 14, 12), -4)]:  # the no-tzdata fallback
    assert ref.utcoffset(dt.datetime(*args)) == dt.timedelta(hours=off), args
try:
    from zoneinfo import ZoneInfo
    z = ZoneInfo("America/New_York")
    for d in (dt.datetime(2026, 11, 1, 12), dt.datetime(2027, 3, 14, 12), dt.datetime(2026, 7, 4, 3), dt.datetime(2026, 12, 25, 9)):
        assert ref.utcoffset(d) == z.utcoffset(d), d  # fallback agrees with the real database
except Exception:
    pass  # no tz database on this machine: the fallback is what runs here
assert feed.when("2026-10-23", TZ, True) == dt.datetime(2026, 10, 23, 23, 59, 59, tzinfo=TZ) and feed.when("2026-10-23", TZ) == dt.datetime(2026, 10, 23, tzinfo=TZ)
assert feed.when("2026-10-23T08:00", TZ) == ny(2026, 10, 23, 8) and feed.when(None, TZ) is None

# --- ordering: next, then time-sensitive soonest-expiring, then evergreen oldest first
old, new = item(), item()
soon, later = item(fu=NOW + 30 * H), item(fu=NOW + 60 * H)
nxt = item(prio="next")
assert ids(run([old, new, later, soon, nxt])) == [nxt["id"], soon["id"], later["id"], old["id"], new["id"]]
# carousels never take the Reel slot and vice versa
r = item("reel")
got = dict(sends(run([old, r])))
assert got[r["id"]] == ny(2026, 10, 15, 16) and got[old["id"]] == ny(2026, 10, 15, 12)

# --- expiry: past fresh_until -> expire (and never sent); a slot after fresh_until is skipped, an earlier one is fine
dead, tight = item(fu=NOW - H), item(fu=ny(2026, 10, 15, 13))
a = run([dead, tight, old])
assert [(x["do"], x["id"]) for x in a if x["do"] == "expire"] == [("expire", dead["id"])] and dead["id"] not in ids(a)
assert dict(sends(a))[tight["id"]] == ny(2026, 10, 15, 12) and dict(sends(a))[old["id"]] == ny(2026, 10, 15, 19)
assert run([item(fu=ny(2026, 10, 15, 8))])[0]["do"] == "expire" and run([item(fu=ny(2026, 10, 15, 11))]) == []  # not expired yet, but no slot is early enough
flagged = {**item(), "expired": True}
assert run([flagged]) == []  # already expired.json

# --- not_before: a scorecard waits for release day
sc = item(nb=ny(2026, 10, 16, 0))
assert dict(sends(run([sc])))[sc["id"]] == ny(2026, 10, 16, 12)
assert run([sc], hours=20) == []

# --- promos: ahead of organic once eligible, preferred_date respected, 5 organic + 48h between promos
pr1, pr2 = item(kind="promo"), item(kind="promo")
org = [item() for _ in range(12)]
a = run([pr1, pr2, *org], hours=24 * 6)
got = dict(sends(a))
assert got[pr1["id"]] == ny(2026, 10, 15, 12), "first promo takes the first slot"
assert got[pr2["id"]] == ny(2026, 10, 18, 12), got[pr2["id"]]  # slots d0 12(promo) 19 | d1 12 19 | d2 12 19 = 5 organic, then >= 48h
pref = item(kind="promo", pref=dt.date(2026, 10, 17))
assert dict(sends(run([pref, *org], hours=24 * 6)))[pref["id"]] == ny(2026, 10, 17, 12)  # not before its preferred date
pin_promo = [feed.entry(ny(2026, 10, 15, 12), "carousel", "promo", "x", ["z"], "b0")]  # a promo already scheduled right before
a = run([pr1, *org], pin_promo, hours=24 * 6)
assert dict(sends(a))[pr1["id"]] == ny(2026, 10, 18, 12)
pin_promo_later = [feed.entry(ny(2026, 10, 17, 19), "carousel", "promo", "x", ["z"], "b0")]  # and one scheduled later: needs room on that side too
a = run([pr1, *org], pin_promo_later, hours=24 * 6)  # conservative: only organic posts that already exist count, so it waits until after the later promo
assert dict(sends(a))[pr1["id"]] == ny(2026, 10, 20, 19), a
tight_later = [feed.entry(ny(2026, 10, 16, 19), "carousel", "promo", "x", ["z"], "b0")]  # a promo 31h after the first slot: no slot has room on both sides
assert pr1["id"] not in ids(run([pr1, *org], tight_later, hours=24 * 2))

# --- variety: no same style / lead artist back to back (across formats), else the slot stays empty unless it is < 12h away
pin = [feed.entry(ny(2026, 10, 17, 16), "reel", "organic", "limelight", ["Ashe"], "b1")]
x, y = item(style="limelight", artist="Z"), item(style="explainer", artist="ashe")
z = item(style="explainer", artist="Q")
a = run([x], pin, hours=24 * 3 + 8)
assert all(d != ny(2026, 10, 17, 12) and d != ny(2026, 10, 17, 19) for _, d in sends(a)), "style clash with the neighbouring Reel left empty"
a = run([y], pin, hours=24 * 3 + 8)
assert all(d not in (ny(2026, 10, 17, 12), ny(2026, 10, 17, 19)) for _, d in sends(a)), "same lead artist (case-insensitive) left empty"
assert dict(sends(run([x, z], pin, hours=24 * 3 + 8)))[z["id"]] == ny(2026, 10, 15, 12)  # picks the best candidate that passes
# two posts planned in a row also respect it
p, q, w = item(style="same", artist="A"), item(style="same", artist="B"), item(style="other", artist="C")
a = dict(sends(run([p, q, w])))
assert a[p["id"]] < a[w["id"]] < a[q["id"]], a  # p, w (breaks the repeat), then q
# < 12h away: a repeat beats an empty slot, with a warning
near_pin = [feed.entry(ny(2026, 10, 15, 16), "reel", "organic", "limelight", ["Ashe"], "b1")]
a = run([x], near_pin, hours=9)
assert sends(a) == [(x["id"], ny(2026, 10, 15, 12))] and a[0]["warn"], a  # 12:00 is 3h away and next to the Reel
far_pin = [feed.entry(ny(2026, 10, 16, 16), "reel", "organic", "limelight", ["Ashe"], "b1")]
assert [d for _, d in sends(run([x], far_pin, hours=40)) if d == ny(2026, 10, 16, 12)] == [] and run([x], far_pin, hours=40)[0]["id"] == x["id"]

# --- bump: "next" takes the next slot of its format over a normal pool post; pinned posts, promos and soon posts stay
b = item(sent={"id": "bb", "dueAt": "x"})
occ_pool = feed.entry(ny(2026, 10, 15, 12), "carousel", "organic", b["style"], b["artists"], "bb", b["id"])
nx = item(prio="next")
a = run([b, nx], [occ_pool])
assert a[0] == {"do": "send", "id": nx["id"], "due": ny(2026, 10, 15, 12), "warn": None, "drop": [{"id": b["id"], "buffer_id": "bb"}]}, a
assert dict(sends(a))[b["id"]] == ny(2026, 10, 15, 19), "the bumped post goes back to the pool and takes the following slot"
legacy = feed.entry(ny(2026, 10, 15, 12), "carousel", "organic", "limelight", ["Ashe"], "L1")
a = run([nx], [legacy])
assert a == [{"do": "send", "id": nx["id"], "due": ny(2026, 10, 15, 19), "warn": None, "drop": []}], a  # legacy slot untouched
promo_occ = feed.entry(ny(2026, 10, 15, 12), "carousel", "promo", "x", ["z"], "bp", "pp")
assert sends(run([nx, {**item(kind="promo", sent={"id": "bp"}), "id": "pp"}], [promo_occ]))[0][1] == ny(2026, 10, 15, 19)
close = dt.datetime(2026, 10, 15, 15, 40, tzinfo=UTC)  # 11:40 in New York: the 12:00 slot is 20 minutes away
a = run([b, nx], [occ_pool], now=close)
assert [x for x in sends(a) if x[0] == nx["id"]][0][1] == ny(2026, 10, 15, 19) and not any(x["drop"] for x in a)
nxt_reel = item("reel", prio="next")
assert run([nxt_reel], [occ_pool])[0]["due"] == ny(2026, 10, 15, 16)  # only the slots of its own format count
# a "next" post already sitting in Buffer on a later day moves up to the first slot
far = item(prio="next", sent={"id": "bf", "dueAt": "x"})
far_e = feed.entry(ny(2026, 10, 16, 12), "carousel", "organic", far["style"], far["artists"], "bf", far["id"])
a = run([far], [far_e])
assert a == [{"do": "send", "id": far["id"], "due": ny(2026, 10, 15, 12), "warn": None, "drop": [{"id": far["id"], "buffer_id": "bf"}]}], a
here = item(prio="next", sent={"id": "bh", "dueAt": "x"})
assert run([here], [feed.entry(ny(2026, 10, 15, 12), "carousel", "organic", here["style"], here["artists"], "bh", here["id"])]) == []  # already first

# --- pinned legacy slots are never filled or dropped
pins = [feed.entry(d, f, "organic", None, [], f"L{i}") for i, (d, f) in enumerate(feed.slots_between(NOW, SCHED))]
assert run([old, nx, r], pins) == []

# --- Buffer room: stop when full, a bump needs no room
a = run([item() for _ in range(5)], room=2)
assert len(sends(a)) == 2
assert len(sends(run([b, nx], [occ_pool], room=0))) == 1
print("ok")
