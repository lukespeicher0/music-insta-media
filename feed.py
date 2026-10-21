"""Runs every 6 hours (and on every push of a pool.json / buffer.json) on GitHub Actions: keeps Buffer's queue topped up, so the PC can be off.

Two kinds of posts live in this repo:
  * POOL posts  <id>/pool.json   (id = pYYMMDD-HHMM-slug): no time of their own. plan() assigns them to the daily slots (America/New_York) for the
    next assign_hours, best fit first. Slots: schedule.json slot_pattern (alternating days of [time, format]) from pattern_anchor on; before it
    (or without a pattern) post_times = carousels + reel_time = Reel.
  * LEGACY slot posts  YYYY-MM-DD-HHMM/buffer.json: a fixed dueAt ("pinned"). Fed to Buffer as before, never moved or bumped.
Buffer's free plan holds 10 scheduled posts per channel. A sent post gets <id>/sent.json; a pool post that went stale gets expired.json.
Settings come from schedule.json (written by insta.py from config.json); `python feed.py --dry` prints the plan without writing anything.
"""
import datetime as dt, glob, json, os, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "Mozilla/5.0"}
UTC = dt.timezone.utc
EPOCH = dt.datetime(1970, 1, 1, tzinfo=UTC)
DEFAULTS = {"post_times": ["12:00", "19:00"], "reel_time": "16:00", "timezone": "America/New_York", "assign_hours": 72, "buffer_limit": 10,
            "promo": {"min_organic": 5, "min_hours": 48}, "pinned": {}, "reel_phone_mode": False}


# ---------- time ----------

class _NY(dt.tzinfo):
    """US Eastern rules for machines with no tz database (Windows has none unless the tzdata package is installed).
    ponytail: wall-time rule, so conversions are an hour off only inside the 1-3am switch window; slots are never there."""
    def _dst(self, d):
        y, d = d.year, d.replace(tzinfo=None)
        start = dt.datetime(y, 3, 8 + (6 - dt.date(y, 3, 8).weekday()) % 7, 2)  # second Sunday of March, 2am
        end = dt.datetime(y, 11, 1 + (6 - dt.date(y, 11, 1).weekday()) % 7, 2)  # first Sunday of November, 2am
        return start <= d < end

    def utcoffset(self, d):
        return dt.timedelta(hours=-4 if self._dst(d) else -5)

    def dst(self, d):
        return dt.timedelta(hours=1 if self._dst(d) else 0)

    def tzname(self, d):
        return "EDT" if self._dst(d) else "EST"


def tzinfo(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        assert name == "America/New_York", f"no tz data for {name} (pip install tzdata)"
        return _NY()


def parse(s):
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(UTC)


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def when(s, tz, end=False):
    """ISO date/datetime -> aware datetime. Local time unless it carries an offset; a bare date means the start of that day (end=True: its last second)."""
    if not s:
        return None
    t = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    if len(str(s)) <= 10 and end:
        t = t.replace(hour=23, minute=59, second=59)
    return t if t.tzinfo else t.replace(tzinfo=tz)


def slot_name(due, tz):
    return due.astimezone(tz).strftime("%Y-%m-%d-%H%M")


def schedule(root=None):
    f = Path(root or HERE) / "schedule.json"
    return {**DEFAULTS, **(json.loads(f.read_text(encoding="utf-8")) if f.exists() else {})}


def day_slots(day, sched):
    """[(HH:MM, 'carousel'|'reel')] posted on one local date: slot_pattern[(days since pattern_anchor) % len] from the anchor on, post_times + reel_time before it or without a pattern."""
    pat, a = sched.get("slot_pattern"), sched.get("pattern_anchor")
    if pat and a and day >= dt.date.fromisoformat(a):
        return [tuple(x) for x in pat[(day - dt.date.fromisoformat(a)).days % len(pat)]]
    return [(t, "carousel") for t in sched["post_times"]] + [(sched["reel_time"], "reel")]


def slots_between(now, sched, hours=None, skip=15):
    """[(due UTC, 'carousel'|'reel')] for every slot from now+skip minutes to now+hours, in sched's timezone (DST-aware)."""
    tz, hours = tzinfo(sched["timezone"]), hours or sched["assign_hours"]
    lo, hi = now + dt.timedelta(minutes=skip), now + dt.timedelta(hours=hours)
    out = []
    for n in range(hours // 24 + 3):
        day = now.astimezone(tz).date() + dt.timedelta(days=n - 1)
        for t, fmt in day_slots(day, sched):
            h, m = map(int, t.split(":"))
            due = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz).astimezone(UTC)
            if lo <= due <= hi:
                out.append((due, fmt))
    return sorted(out)


def days_of(sched, fmt, have, start, cap=60):
    """Days of content `have` posts of one format cover, walking the real pattern from date `start`: a day needs as many as it has slots of that format (0 = free)."""
    n = 0.0
    for k in range(cap):
        need = sum(1 for _, f in day_slots(start + dt.timedelta(days=k), sched) if f == fmt)
        if have >= need:
            have, n = have - need, n + 1
        else:
            return n + have / need
    return n


def mode(phone):
    """Buffer schedulingType for a Reel: 'notification' = Buffer's phone app reminds the owner at post time, 'automatic' = Buffer posts it."""
    return "notification" if phone else "automatic"


def caption(i, how):
    """The text Buffer gets: in notification mode a Reel with a "sound" ({artist, title}: the song to pick in Instagram's music library) gets '🎵 Artist – Title' appended
    (cut to fit 2200 chars; the caption itself is never trimmed). Automatic mode posts the royalty-free track baked into the video, so no line."""
    text, sound = i["input"]["text"], i.get("sound")
    if how != "notification" or i["format"] != "reel" or not (sound and sound.get("artist") and sound.get("title")):
        return text
    line = f"\n\n\U0001F3B5 {sound['artist']} – {sound['title']}"
    room = 2200 - len(text)
    return text + (line if len(line) <= room else line[:room] if room > 8 else "")


def wrong_mode(items, now, phone, gap=30):
    """Pool Reels already in Buffer whose recorded mode (sent.json; none = automatic) differs from the setting and that are > gap minutes away: re-send them."""
    return [i for i in items if i["format"] == "reel" and i["sent"] and i["sent"].get("dueAt") and (i["sent"].get("mode") or "automatic") != mode(phone)
            and parse(i["sent"]["dueAt"]) > now + dt.timedelta(minutes=gap)]


# ---------- the pool ----------

def load_pool(root, tz):
    """Every <id>/pool.json as an item: format, kind organic|promo, style, artists, priority, fresh_until/not_before/added (aware), sent {id,dueAt}|None, expired."""
    items = []
    for f in sorted(Path(root).glob("*/pool.json")):
        d = f.parent
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            j["input"], j["format"]
        except (ValueError, KeyError) as e:  # one bad file must not stop the feeder
            print("skipping unreadable", f, e)
            continue
        sent = json.loads((d / "sent.json").read_text(encoding="utf-8")) if (d / "sent.json").exists() else None
        pref = j.get("preferred_date")
        items.append({"id": d.name, "dir": d, "org": j.get("org"), "input": j["input"], "format": j["format"], "kind": j.get("kind") or "organic",
                      "style": j.get("style"), "artists": j.get("artists") or [], "sound": j.get("sound"), "silent": bool(j.get("silent")), "priority": j.get("priority") or "normal",
                      "fresh_until": when(j.get("fresh_until"), tz, True), "not_before": when(j.get("not_before"), tz),
                      "added": when(j.get("added"), tz) or EPOCH, "preferred_date": dt.date.fromisoformat(str(pref)[:10]) if pref else None,
                      "sent": sent if sent and sent.get("id") else None, "expired": (d / "expired.json").exists()})
    return items


def entry(due, fmt, kind="organic", style=None, artists=(), bid=None, pool=None):
    """One post on the timeline (already in Buffer, pinned, or just planned). bid = Buffer id; pool = the pool item's id."""
    return {"id": bid, "due": due, "format": fmt, "kind": kind, "style": style, "artists": list(artists), "pool": pool}


def pool_entries(items, now, days=14):
    """Timeline entries for pool posts that were sent: upcoming ones carry their Buffer id; recent past ones (id None) only feed the ratio/variety checks."""
    out = []
    for i in items:
        if i["sent"] and i["sent"].get("dueAt"):
            due = parse(i["sent"]["dueAt"])
            if due > now - dt.timedelta(days=days):
                out.append(entry(due, i["format"], i["kind"], i["style"], i["artists"], i["sent"]["id"] if due > now else None, i["id"]))
    return out


def rank(it):
    """Pick order: 'next' first, then promos (only eligible ones reach here), then time-sensitive soonest-expiring, then evergreen oldest first."""
    fu = it["fresh_until"]
    return (it["priority"] != "next", it["kind"] != "promo", fu is None, fu or it["added"], it["added"], it["id"])


def promo_ok(due, tl, rules):
    """A promo needs >= min_organic organic posts and >= min_hours from the nearest promo on each side (scheduled or planned)."""
    need, gap = rules.get("promo_min_organic", 5), dt.timedelta(hours=rules.get("promo_min_hours", 48))
    promos = [e for e in tl if e["kind"] == "promo"]
    for p in (max((e for e in promos if e["due"] < due), key=lambda e: e["due"], default=None),
              min((e for e in promos if e["due"] > due), key=lambda e: e["due"], default=None)):
        if p:
            lo, hi = sorted((p["due"], due))
            if hi - lo < gap or sum(1 for e in tl if e["kind"] != "promo" and lo < e["due"] < hi) < need:
                return False
    return True


def clash(it, due, tl):
    """Same style or same lead artist as the post right before or right after this time (any format)."""
    lead = (it["artists"] or [None])[0]
    lead = lead.lower() if lead else None
    for e in (max((e for e in tl if e["due"] < due), key=lambda e: e["due"], default=None),
              min((e for e in tl if e["due"] > due), key=lambda e: e["due"], default=None)):
        if e:
            el = (e["artists"] or [None])[0]
            if (it["style"] and e["style"] == it["style"]) or (lead and el and el.lower() == lead):
                return True
    return False


def plan(items, scheduled, slots, now, rules):
    """Pure: decide what to do. items = load_pool(); scheduled = timeline entries (pinned + sent pool posts; see entry()); slots = [(due, format)] in time order;
    rules = {tz, room (free Buffer places, None = unlimited), urgent_hours 12, bump_minutes 30, promo_min_organic 5, promo_min_hours 48}.
    -> [{"do": "expire", "id"} | {"do": "send", "id", "due", "drop": [{"id": item id, "buffer_id"}], "warn": str|None}] in the order to run them.
    ponytail: no entry exists for the post played just before `now`, so the first slot's back-to-back check only sees what is still scheduled."""
    tz, room = rules["tz"], rules.get("room")
    urgent, bump = dt.timedelta(hours=rules.get("urgent_hours", 12)), dt.timedelta(minutes=rules.get("bump_minutes", 30))
    acts, pool, placed = [], [], set()
    for i in (dict(i) for i in items):
        if i["expired"]:
            continue
        if not i["sent"] and i["fresh_until"] and i["fresh_until"] <= now:
            acts.append({"do": "expire", "id": i["id"]})
        else:
            pool.append(i)
    by = {i["id"]: i for i in pool}
    tl = sorted((dict(e) for e in scheduled), key=lambda e: e["due"])
    occ = {e["due"]: e for e in tl}

    def fits(i, due):  # a silent Reel only goes out as a phone reminder (the owner adds the song in Instagram)
        return ((not i.get("silent") or rules.get("phone")) and (not i["not_before"] or i["not_before"] <= due) and (not i["fresh_until"] or due <= i["fresh_until"])
                and (i["kind"] != "promo" or not i["preferred_date"] or i["preferred_date"] <= due.astimezone(tz).date()))

    def put(i, due, drop, warn=None):
        nonlocal room
        for x in drop:  # bumped or moved posts leave the timeline; a bumped pool post goes back to the pool
            tl.remove(x)
            del occ[x["due"]]
            if x["pool"] in by:
                by[x["pool"]]["sent"] = None
        occ[due] = entry(due, i["format"], i["kind"], i["style"], i["artists"], None, i["id"])
        tl.append(occ[due])
        placed.add(i["id"])
        if not drop and room is not None:
            room -= 1
        acts.append({"do": "send", "id": i["id"], "due": due, "warn": warn, "drop": [{"id": x["pool"], "buffer_id": x["id"]} for x in drop]})

    # 1. "Post next": into the very next slot of its format, bumping a normal pool post (never a pinned/legacy post or a promo) that is > bump_minutes away.
    #    Variety is waived: the owner asked for it.
    for i in sorted((i for i in pool if i["priority"] == "next"), key=lambda i: (i["added"], i["id"])):
        for due, fmt in slots:
            if fmt != i["format"] or not fits(i, due):
                continue
            e, mine = occ.get(due), next((x for x in tl if x["pool"] == i["id"] and x["id"]), None)  # mine: its own place in Buffer, if any
            if e is not None and e is mine:
                break  # already in the first place it could go
            drop = [x for x in (mine, e) if x]
            if e and not (e["pool"] in by and e["id"] and e["kind"] != "promo" and by[e["pool"]]["priority"] != "next" and e["due"] > now + bump):
                continue  # pinned, a promo, within 30 minutes or another "next" post: try the following slot
            if not drop and room is not None and room <= 0:
                break
            if i["kind"] == "promo" and not promo_ok(due, [x for x in tl if not any(x is d for d in drop)], rules):
                continue
            put(i, due, drop)
            break

    # 2. fill the empty slots in time order
    for due, fmt in slots:
        if due in occ:
            continue
        if room is not None and room <= 0:
            break
        c = sorted((i for i in pool if i["format"] == fmt and not i["sent"] and i["id"] not in placed and fits(i, due)
                    and (i["kind"] != "promo" or promo_ok(due, tl, rules))), key=rank)
        pick = next((i for i in c if not clash(i, due, tl)), None)
        warn = None
        if not pick and c and due - now < urgent:  # an empty slot this close is worse than a repeat
            pick, warn = c[0], "repeats the style or lead artist of a neighbouring post"
        if pick:
            put(pick, due, [], warn)
    return acts


# ---------- Buffer ----------

def buffer(query, variables=None):
    req = urllib.request.Request("https://api.buffer.com", method="POST",
                                 data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Authorization": f"Bearer {os.environ['BUFFER_API_KEY']}", "Content-Type": "application/json", **UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if out.get("errors"):
        raise SystemExit(f"Buffer error: {out['errors']}")
    return out["data"]


def live(url):
    for _ in range(24):  # Pages deploys a fresh push in ~30-90s
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA), timeout=20) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(10)
    return False


def scheduled(org, channel):
    q = (f'query{{posts(first:100,input:{{organizationId:"{org}",filter:{{status:[scheduled],channelIds:["{channel}"]}}}})'
         '{edges{node{id dueAt text}}}}')
    return [e["node"] for e in buffer(q)["posts"]["edges"]]


def delete(bid):
    res = buffer("mutation($in:DeletePostInput!){deletePost(input:$in){__typename ... on MutationError{message}}}", {"in": {"id": bid}})["deletePost"]
    if res.get("message"):
        raise SystemExit(f"Buffer refused delete {bid}: {res['message']}")


def write(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


CREATE = """mutation($in:CreatePostInput!){createPost(input:$in){
    ... on PostActionSuccess{post{id dueAt text}} ... on MutationError{message}}}"""


def main(dry=False):
    """dry: read Buffer, print what would happen, write nothing."""
    now = dt.datetime.now(UTC)
    sched = schedule()
    tz = tzinfo(sched["timezone"])
    limit = int(os.environ.get("BUFFER_LIMIT") or sched["buffer_limit"])
    items = load_pool(HERE, tz)
    legacy = []
    for f in glob.glob(str(HERE / "*/buffer.json")):
        d = Path(f).parent
        if not (d / "sent.json").exists():
            legacy.append((json.loads(Path(f).read_text(encoding="utf-8")), d))
    legacy.sort(key=lambda x: x[0]["input"]["dueAt"])
    if not (legacy or any(not i["expired"] for i in items)):
        return print("nothing waiting")
    org, channel = (legacy[0][0]["org"], legacy[0][0]["input"]["channelId"]) if legacy else (items[0]["org"], items[0]["input"]["channelId"])
    queue = scheduled(org, channel)
    known = {i["sent"]["id"] for i in items if i["sent"]}  # pool posts already in Buffer: never a legacy post's "leftover"

    # legacy slot posts (fixed dueAt): same rules as ever
    pending, blocked = [], False  # pending: legacy posts still waiting for room; their slot stays reserved
    for job, d in legacy:
        inp = job["input"]
        due = dt.datetime.fromisoformat(inp["dueAt"].replace("Z", "+00:00"))
        if due < now + dt.timedelta(minutes=10):
            if not dry:
                write(d / "sent.json", {"missed": True, "at": now.isoformat()})
            print("missed", d.name)
            continue
        same = [p for p in queue if p["dueAt"] == inp["dueAt"]]
        mine = next((p for p in same if p["text"].strip() == inp["text"].strip()), None)
        for p in same:  # ponytail: one post per slot; anything else at this exact minute (and not a known pool post) is a vetoed post's leftover
            if p is not mine and p["id"] not in known:
                print("stale post at", inp["dueAt"], "would be deleted" if dry else "deleted")
                if not dry:
                    delete(p["id"])
                queue.remove(p)
        if mine:  # created on an earlier run whose commit didn't land: just record it
            if not dry:
                write(d / "sent.json", {"id": mine["id"], "dueAt": mine["dueAt"]})
            continue
        if blocked or len(queue) >= limit:
            print("Buffer full, waiting:", d.name)
            pending.append(job)
            blocked = True
            continue
        if dry:
            print("would send legacy", d.name)
            queue.append({"id": "dry-" + d.name, "dueAt": inp["dueAt"], "text": inp["text"]})
            continue
        if not all(live(a.get("image", a.get("video", {})).get("url")) for a in inp["assets"]):
            print("media not live yet:", d.name)
            pending.append(job)
            blocked = True
            continue
        res = buffer(CREATE, {"in": inp})["createPost"]
        if "post" not in res:
            print("Buffer refused", d.name, res)
            pending.append(job)
            blocked = True
            continue
        write(d / "sent.json", {"id": res["post"]["id"], "dueAt": res["post"]["dueAt"]})
        queue.append(res["post"])
        print("sent", d.name, res["post"]["dueAt"])

    # pool posts
    texts = {caption(i, m).strip(): i for i in items if not i["sent"] and not i["expired"] for m in ("automatic", "notification")}  # either mode's text is ours
    for p in queue:  # a pool post we created whose sent.json never landed: adopt it instead of making a duplicate
        i = texts.get(p["text"].strip())
        if i and p["id"] not in known and not i["sent"]:
            i["sent"] = {"id": p["id"], "dueAt": p["dueAt"]}
            if not dry:
                write(i["dir"] / "sent.json", i["sent"])
    ids = {p["id"] for p in queue}
    for i in items:  # sent for a future slot but no longer in Buffer (deleted by hand): back to the pool
        if i["sent"] and i["sent"].get("dueAt") and parse(i["sent"]["dueAt"]) > now + dt.timedelta(minutes=15) and i["sent"]["id"] not in ids:
            print("gone from Buffer, back in the pool:", i["id"])
            if not dry:
                (i["dir"] / "sent.json").unlink(missing_ok=True)
            i["sent"] = None
    phone = bool(sched.get("reel_phone_mode"))
    for i in wrong_mode(items, now, phone):  # the phone-mode switch was flipped: Reels already in Buffer must follow it (same slot), else a notification Reel never posts itself
        old, due = i["sent"], parse(i["sent"]["dueAt"])
        print("re-sending", i["id"], "as", mode(phone), "(was", (old.get("mode") or "automatic") + ")", "- dry" if dry else "")
        if dry:
            continue
        delete(old["id"])
        (i["dir"] / "sent.json").unlink(missing_ok=True)
        queue = [p for p in queue if p["id"] != old["id"]]
        i["sent"] = None
        if i.get("silent") and not phone:  # phone mode switched off: a silent Reel must not auto-post; back to the pool until phone mode is on again
            print("silent Reel back to the pool:", i["id"])
            continue
        res = buffer(CREATE, {"in": {**i["input"], "text": caption(i, mode(phone)), "dueAt": iso(due), "schedulingType": mode(phone)}})["createPost"]
        if "post" not in res:
            print("Buffer refused", i["id"], res)  # no sent.json: it is back in the pool and the planner places it again
            continue
        i["sent"] = {"id": res["post"]["id"], "dueAt": res["post"]["dueAt"], "mode": mode(phone)}
        write(i["dir"] / "sent.json", i["sent"])
        queue.append(res["post"])
    by_sent = {i["sent"]["id"]: i for i in items if i["sent"]}
    pin = sched.get("pinned", {})

    def pinned(due, bid=None):
        m = pin.get(slot_name(due, tz), {})
        return entry(due, "reel" if due.astimezone(tz).strftime("%H:%M") == sched["reel_time"] else "carousel",
                     "promo" if m.get("paid") else "organic", m.get("style"), m.get("artists", []), bid)

    entries, stale = [], []
    for p in queue:
        due = parse(p["dueAt"])
        if p["id"] in by_sent:
            i = by_sent[p["id"]]
            entries.append(entry(due, i["format"], i["kind"], i["style"], i["artists"], p["id"], i["id"]))
        elif (HERE / slot_name(due, tz)).is_dir() or slot_name(due, tz) in pin:  # a legacy slot post: pinned, never touched
            entries.append(pinned(due, p["id"]))
        else:
            stale.append(p)  # not ours: its minute counts as free, and the post goes if we need that minute
    entries += [pinned(parse(j["input"]["dueAt"])) for j in pending]
    entries += [e for e in pool_entries(items, now) if e["id"] is None]  # recent past pool posts: context for the ratio/variety rules
    rules = {"tz": tz, "room": limit - len(queue), "phone": phone, "urgent_hours": 12, "bump_minutes": 30,
             "promo_min_organic": sched["promo"]["min_organic"], "promo_min_hours": sched["promo"]["min_hours"]}
    acts = plan(items, entries, slots_between(now, sched), now, rules)
    if not acts:
        print(f"pool: nothing to do (Buffer {len(queue)}/{limit}, {sum(1 for i in items if not i['sent'] and not i['expired'])} waiting)")
    byid = {i["id"]: i for i in items}
    for a in acts:
        i = byid[a["id"]]
        if a["do"] == "expire":
            print("expired", i["id"])
            if not dry:
                write(i["dir"] / "expired.json", {"at": now.isoformat()})
            continue
        due = a["due"]
        print("would send" if dry else "sending", i["id"], iso(due), f"bumping {[d['id'] for d in a['drop']]}" if a["drop"] else "",
              f"WARNING: {a['warn']}" if a["warn"] else "")
        if dry:
            continue
        if not all(live(x.get("image", x.get("video", {})).get("url")) for x in i["input"]["assets"]):
            print("media not live yet:", i["id"])
            continue
        for d in a["drop"]:  # a bumped post returns to the pool
            delete(d["buffer_id"])
            if d["id"] in byid:
                (byid[d["id"]]["dir"] / "sent.json").unlink(missing_ok=True)
        for p in [p for p in stale if parse(p["dueAt"]) == due]:
            delete(p["id"])
        how = mode(phone) if i["format"] == "reel" else "automatic"  # carousels always post themselves
        res = buffer(CREATE, {"in": {**i["input"], "text": caption(i, how), "dueAt": iso(due), "schedulingType": how}})["createPost"]
        if "post" not in res:
            print("Buffer refused", i["id"], res)
            break
        write(i["dir"] / "sent.json", {"id": res["post"]["id"], "dueAt": res["post"]["dueAt"], "mode": how})
        if i["priority"] == "next":  # the wish is granted
            f = i["dir"] / "pool.json"
            j = json.loads(f.read_text(encoding="utf-8"))
            j["priority"] = "normal"
            f.write_text(json.dumps(j, indent=1, ensure_ascii=False), encoding="utf-8")
        print("sent", i["id"], res["post"]["dueAt"])


if __name__ == "__main__":
    main(dry="--dry" in sys.argv)
