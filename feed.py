"""Runs every 6 hours (and on every new post) on GitHub Actions: keeps Buffer's queue topped up from */buffer.json, so the PC can be off.

Buffer's free plan holds 10 scheduled posts per channel; we queue days ahead here and feed it the soonest
posts whenever a slot frees up. Each sent post gets a sent.json next to its buffer.json.
"""
import datetime as dt, glob, json, os, time, urllib.request
from pathlib import Path

LIMIT = int(os.environ.get("BUFFER_LIMIT", "10"))
UA = {"User-Agent": "Mozilla/5.0"}


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


def main():
    now = dt.datetime.now(dt.timezone.utc)
    todo = []
    for f in glob.glob("*/buffer.json"):
        d = Path(f).parent
        if not (d / "sent.json").exists():
            todo.append((json.loads(Path(f).read_text(encoding="utf-8")), d))
    todo.sort(key=lambda x: x[0]["input"]["dueAt"])
    if not todo:
        return print("nothing waiting")
    org, channel = todo[0][0]["org"], todo[0][0]["input"]["channelId"]
    queue = scheduled(org, channel)
    for job, d in todo:
        inp = job["input"]
        due = dt.datetime.fromisoformat(inp["dueAt"].replace("Z", "+00:00"))
        if due < now + dt.timedelta(minutes=10):
            (d / "sent.json").write_text(json.dumps({"missed": True, "at": now.isoformat()}))
            print("missed", d)
            continue
        same = [p for p in queue if p["dueAt"] == inp["dueAt"]]
        mine = next((p for p in same if p["text"].strip() == inp["text"].strip()), None)
        for p in same:  # ponytail: one post per slot; anything else at this exact minute is a vetoed post's leftover
            if p is not mine:
                buffer("mutation($in:DeletePostInput!){deletePost(input:$in){__typename}}", {"in": {"id": p["id"]}})
                queue.remove(p)
        if mine:  # created on an earlier run whose commit didn't land: just record it
            (d / "sent.json").write_text(json.dumps({"id": mine["id"], "dueAt": mine["dueAt"]}))
            continue
        if len(queue) >= LIMIT:
            print("Buffer full, waiting:", d)
            break
        urls = [a.get("image", a.get("video", {})).get("url") for a in inp["assets"]]
        if not all(live(u) for u in urls):
            print("media not live yet:", d)
            break
        res = buffer("""mutation($in:CreatePostInput!){createPost(input:$in){
            ... on PostActionSuccess{post{id dueAt text}} ... on MutationError{message}}}""", {"in": inp})["createPost"]
        if "post" not in res:
            print("Buffer refused", d, res)
            break
        (d / "sent.json").write_text(json.dumps({"id": res["post"]["id"], "dueAt": res["post"]["dueAt"]}))
        queue.append(res["post"])
        print("sent", d, res["post"]["dueAt"])


if __name__ == "__main__":
    main()
