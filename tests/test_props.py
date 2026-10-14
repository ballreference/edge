"""Fake a day of NFL props and check the pull budget, leg grading and parlays."""
import json, random, shutil, sys, tempfile, urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import edge

ET = ZoneInfo("America/Detroit")
rng = random.Random(4)
now = datetime(2026, 10, 18, 10, 30, tzinfo=ET)        # a Sunday morning
games = [{"id": f"g{i}", "sport_key": "americanfootball_nfl", "home_team": f"Home{i}", "away_team": f"Away{i}",
          "commence_time": edge.iso(now.replace(hour=13, minute=0) + timedelta(hours=3 * (i % 3)))} for i in range(6)]
books = json.loads((HERE.parent / "config.json").read_text())["books"]
credits = {"left": 300, "calls": 0, "cost": 0}

def props_for(g, markets):
    bks = []
    for b in books:
        mk = []
        for m in markets:
            outs = []
            for j in range(3):
                line = 50.5 + 10 * j
                p = min(.9, max(.1, .5 + rng.gauss(0, .03) + (.07 if b == "fanduel" and j == 0 and g["id"] in ("g0", "g1", "g2") else 0)))
                o, u = edge.fair_american(p - .025), edge.fair_american(1 - p - .025)
                outs += [{"name": "Over", "description": f"P{g['id']}{m[-6:]}{j}", "price": o, "point": line},
                         {"name": "Under", "description": f"P{g['id']}{m[-6:]}{j}", "price": u, "point": line}]
            mk.append({"key": m, "outcomes": outs})
        bks.append({"key": b, "markets": mk})
    return dict(g, bookmakers=bks)

def http(url):
    u = urllib.parse.urlparse(url); q = dict(urllib.parse.parse_qsl(u.query)); parts = u.path.split("/")
    hdr = lambda: {"x-requests-remaining": str(credits["left"]), "x-requests-used": str(300 - credits["left"])}
    if parts[-1] == "events":
        return 200, hdr(), json.dumps(games if "nfl" in u.path else [])
    if parts[-1] == "odds" and "events" in parts:
        gid = parts[parts.index("events") + 1]; m = q["markets"].split(",")
        credits["left"] -= len(m); credits["calls"] += 1; credits["cost"] += len(m)
        return 200, hdr(), json.dumps(props_for(next(g for g in games if g["id"] == gid), m))
    if parts[-1] == "odds":
        return 200, hdr(), json.dumps([])
    if parts[-1] == "scores":
        return 200, hdr(), "[]"
    return 404, {}, ""

work = Path(tempfile.mkdtemp())
shutil.copy(HERE.parent / "config.json", work / "config.json")
(work / "docs" / "data").mkdir(parents=True)
ctx = edge.Ctx(root=work, key="k", http=http, now=now.astimezone(timezone.utc))
edge.run(ctx)
d = json.loads((work / "docs/data/props.json").read_text())
assert credits["calls"] == 6 and credits["cost"] == 18, credits                  # 6 games x 3 markets
assert d["budget"]["spent"] == 18 and d["n_legs"] == 6 * 3 * 3, (d["budget"], d["n_legs"])
top = d["legs"][0]
assert top["book"] == "fanduel" and top["side"] == "Under" and top["ev"] > .05, top  # the planted soft line
assert all(not l["suspect"] or l["ev"] > .15 for l in d["legs"])
p2 = d["parlays"]["2"]
assert p2 and all(len(r["legs"]) == 2 for r in p2)
ids = {l["id"]: l for l in d["legs"]}
for r in p2:
    assert len({ids[i]["event_id"] for i in r["legs"]}) == 2      # never two legs from one game
    assert all(ids[i]["book"] == r["book"] for i in r["legs"])    # one book per ticket
    assert all(not ids[i]["suspect"] for i in r["legs"])           # no too-good-to-be-true legs
print("props:", credits, "| top leg", top["player"], top["side"], top["line"], top["price"], f"{top['ev']:+.3f}",
      "| parlays per size", {k: len(v) for k, v in d["parlays"].items()})
# second run the same day: no new pull
c0 = credits["calls"]; ctx2 = edge.Ctx(root=work, key="k", http=http, now=(now + timedelta(hours=1)).astimezone(timezone.utc)); edge.run(ctx2)
assert credits["calls"] == c0, "pulled twice in one day"
print("all props checks pass")
