"""Simulate a month of NFL + NBA through edge.py against a fake Odds API.

The fake API charges credits the way the real one documents it:
  /sports, /events: free · /odds: markets x ceil(books/10) · /scores?daysFrom: 2
Books quote noisy, vigged prices around a hidden true probability; now and then
one retail book hangs a stale price, which is what the engine should catch.
"""
import json
import math
import random
import shutil
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import edge  # noqa: E402

ET = ZoneInfo("America/Detroit")
rng = random.Random(7)
TEAMS = {
    "americanfootball_nfl": [f"NFL Team {c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"],
    "basketball_nba": [f"NBA Team {c}" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123"],
}
BOOKS = json.loads((HERE.parent / "config.json").read_text())["books"]


def american(p):
    return edge.fair_american(p)


class World:
    def __init__(self, start):
        self.games = {}
        self.credits = 500
        self.calls = {"events": 0, "odds": 0, "scores": 0, "sports": 0}
        self.charged = {"odds": 0, "scores": 0}
        gid = 0
        # NFL: 4 weeks. Thu 8:15, Sun 1:00 x8, Sun 4:25 x3, Sun 8:20, Mon 8:15
        first_thu = start + timedelta(days=(3 - start.weekday()) % 7)
        for wk in range(5):
            thu = first_thu + timedelta(weeks=wk)
            slots = [(thu, 20, 15)] + [(thu + timedelta(days=3), 13, 0)] * 8 + \
                    [(thu + timedelta(days=3), 16, 25)] * 3 + [(thu + timedelta(days=3), 20, 20),
                                                               (thu + timedelta(days=4), 20, 15)]
            teams = TEAMS["americanfootball_nfl"][:]
            rng.shuffle(teams)
            for (d, h, m) in slots:
                t = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
                self._add(f"nfl{gid}", "americanfootball_nfl", teams.pop(), teams.pop(), t, 3.0, 13.5, 44)
                gid += 1
        # NBA: 8 games a night from day 2 on
        for dd in range(1, 31):
            d = start + timedelta(days=dd)
            teams = TEAMS["basketball_nba"][:]
            rng.shuffle(teams)
            for i in range(8):
                h, m = [(19, 0), (19, 30), (20, 0), (22, 0)][i % 4]
                t = datetime(d.year, d.month, d.day, h, m, tzinfo=ET)
                self._add(f"nba{gid}", "basketball_nba", teams.pop(), teams.pop(), t, 2.5, 12.0, 228)
                gid += 1

    def _add(self, gid, sport, home, away, t, hfa, sd, total):
        margin_mean = rng.gauss(hfa, 6)          # true expected home margin
        tot_mean = rng.gauss(total, 5)
        spread = round(margin_mean * 2) / 2       # market's number (home favored if +)
        tline = round(tot_mean * 2) / 2
        hm = rng.gauss(margin_mean, sd)
        tt = rng.gauss(tot_mean, sd * 1.2)
        hs = max(0, round((tt + hm) / 2))
        as_ = max(0, round((tt - hm) / 2))
        self.games[gid] = dict(id=gid, sport=sport, home=home, away=away, t=t,
                               mu=margin_mean, tmu=tot_mean, sd=sd, spread=spread, tline=tline,
                               hs=hs, as_=as_)

    @staticmethod
    def ncdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    def quote(self, g, now):
        """Every book's two-way lines for one game at time `now`."""
        books = []
        p_home_ml = self.ncdf(g["mu"] / g["sd"])
        p_home_cover = self.ncdf((g["mu"] - g["spread"]) / g["sd"])
        p_over = self.ncdf((g["tmu"] - g["tline"]) / (g["sd"] * 1.2))
        hours = (g["t"] - now).total_seconds() / 3600
        for b in BOOKS:
            sharp = b in ("pinnacle", "lowvig", "betonlineag")
            noise = 0.006 if sharp else 0.014
            vig = 0.02 if sharp else 0.045
            stale = (not sharp) and rng.random() < 0.05 and hours > 2
            mk = []
            for key, p, a, bn, pt in (("h2h", p_home_ml, g["home"], g["away"], None),
                                      ("spreads", p_home_cover, g["home"], g["away"], -g["spread"]),
                                      ("totals", p_over, "Over", "Under", g["tline"])):
                q = min(max(p + rng.gauss(0, noise) + (rng.choice([-1, 1]) * 0.06 if stale else 0), 0.03), 0.97)
                pa, pb = q + vig / 2, (1 - q) + vig / 2
                outs = [{"name": a, "price": american(pa)}, {"name": bn, "price": american(pb)}]
                if key == "spreads":
                    outs[0]["point"], outs[1]["point"] = pt, -pt
                if key == "totals":
                    outs[0]["point"] = outs[1]["point"] = pt
                mk.append({"key": key, "outcomes": outs})
            books.append({"key": b, "last_update": edge.iso(now), "markets": mk})
        return books

    def ev_obj(self, g):
        return {"id": g["id"], "sport_key": g["sport"], "commence_time": edge.iso(g["t"]),
                "home_team": g["home"], "away_team": g["away"]}

    def http(self, now_fn):
        def call(url):
            u = urllib.parse.urlparse(url)
            q = dict(urllib.parse.parse_qsl(u.query))
            parts = u.path.split("/")
            now = now_fn()
            hdr = lambda: {"x-requests-remaining": str(self.credits), "x-requests-used": str(500 - self.credits)}
            if u.path.endswith("/sports"):
                self.calls["sports"] += 1
                return 200, hdr(), json.dumps([{"key": k, "active": True} for k in TEAMS])
            sport = parts[parts.index("sports") + 1]
            gs = [g for g in self.games.values() if g["sport"] == sport]
            if parts[-1] == "events":
                self.calls["events"] += 1
                live = [self.ev_obj(g) for g in gs if g["t"] > now - timedelta(hours=3)
                        and g["t"] < now + timedelta(days=14)]
                return 200, hdr(), json.dumps(live)
            if parts[-1] == "odds":
                cost = len(q["markets"].split(",")) * math.ceil(len(q["bookmakers"].split(",")) / 10)
                if self.credits - cost < 0:
                    return 401, hdr(), '{"message":"out of credits"}'
                self.credits -= cost
                self.calls["odds"] += 1
                self.charged["odds"] += cost
                out = []
                for g in gs:
                    if g["t"] > now - timedelta(hours=3) and g["t"] < now + timedelta(days=14):
                        o = self.ev_obj(g)
                        o["bookmakers"] = self.quote(g, now)
                        out.append(o)
                return 200, hdr(), json.dumps(out)
            if parts[-1] == "scores":
                cost = 2 if "daysFrom" in q else 1
                self.credits -= cost
                self.calls["scores"] += 1
                self.charged["scores"] += cost
                out = []
                for g in gs:
                    if now - timedelta(days=3) < g["t"] < now:
                        done = now - g["t"] > timedelta(hours=3.5)
                        out.append({"id": g["id"], "completed": done,
                                    "scores": [{"name": g["home"], "score": str(g["hs"])},
                                               {"name": g["away"], "score": str(g["as_"])}] if done else None})
                return 200, hdr(), json.dumps(out)
            return 404, {}, "not found"
        return call


def main():
    work = Path(tempfile.mkdtemp())
    shutil.copy(HERE.parent / "config.json", work / "config.json")
    (work / "docs" / "data").mkdir(parents=True)
    start = datetime(2026, 10, 1, 0, 23, tzinfo=ET)     # month starts Thursday Oct 1
    clock = {"t": start}
    world = World(start)
    http = world.http(lambda: clock["t"].astimezone(timezone.utc))
    kinds = {"routine": 0, "close": 0}
    for h in range(24 * 30):
        clock["t"] = start + timedelta(hours=h)
        ctx = edge.Ctx(root=work, key="fake", http=http, now=clock["t"].astimezone(timezone.utc))
        before = dict(ctx.state["last_pull"])
        edge.run(ctx)
        for s, v in ctx.state["last_pull"].items():
            if before.get(s) != v:
                snap = edge.latest_snapshot(ctx, s)
                kinds[snap["kind"]] += 1
    rec = json.loads((work / "docs/data/record.json").read_text())
    board = json.loads((work / "docs/data/board.json").read_text())
    flags = json.loads((work / "data/flags.json").read_text())["flags"]
    print("credits used:", 500 - world.credits, "left:", world.credits)
    print("charged:", world.charged, "calls:", world.calls, "pull kinds:", kinds)
    print("flags logged:", len(flags), "statuses:",
          {s: sum(f['status'] == s for f in flags) for s in ('open', 'closed', 'graded', 'void')})
    print("overall:", json.dumps(rec["overall"]))
    for k in ("by_sport", "by_market", "by_ev"):
        print(k, {n: (t["n"], t["win_pct"], t["expected_pct"], t["units"], t["beat_close_pct"]) for n, t in rec[k].items()})
    print("board sports:", {s: (e["games"], len(e["flags"]), len(e["watch"])) for s, e in board["sports"].items()})
    print("errors:", board["errors"])
    print("log tail:", *board["log"][-5:], sep="\n  ")
    print("work dir:", work)
    return work


if __name__ == "__main__":
    main()
