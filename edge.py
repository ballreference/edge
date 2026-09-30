#!/usr/bin/env python3
"""Edge: finds sportsbook prices that beat the market's fair price.

Every run (hourly, from GitHub Actions):
  1. Asks The Odds API which games are coming up (free).
  2. Pulls odds only when a pull is due -- a daily routine pull, plus one shortly
     before games start so we capture the closing line. Credits are rationed.
  3. Strips the vig from every book's two-way line, averages them into a fair
     probability (sharp books weighted heavier), and flags any of your books
     paying more than fair by at least `min_ev`.
  4. Logs every flag the first time it appears, records the closing line after
     the game starts, and grades it from the final score.
  5. Writes docs/data/*.json for the phone site.

Standard library only.

  python edge.py run            normal scheduled run
  python edge.py check          first-run check: which books come back, credits left
  python edge.py site           rebuild the site data without calling the API
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
API = "https://api.the-odds-api.com/v4"


# --------------------------------------------------------------------------- #
# Odds math
# --------------------------------------------------------------------------- #
def profit(american: float) -> float:
    """Profit per 1 unit staked."""
    return american / 100.0 if american > 0 else 100.0 / abs(american)


def implied(american: float) -> float:
    return 1.0 / (1.0 + profit(american))


def ev(p: float, american: float) -> float:
    """Expected profit per 1 unit staked, if the true win probability is p."""
    return p * profit(american) - (1.0 - p)


def fair_american(p: float) -> int:
    p = min(max(p, 0.001), 0.999)
    if p >= 0.5:
        return -int(round(100 * p / (1 - p)))
    return int(round(100 * (1 - p) / p))


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Context: config, state, clock and HTTP -- all swappable for tests
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


def urllib_http(url: str) -> tuple[int, dict, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "edge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return e.code, {k.lower(): v for k, v in e.headers.items()}, body


class Ctx:
    def __init__(self, root: Path = ROOT, key: str | None = None,
                 http=urllib_http, now: datetime | None = None):
        self.root = root
        self.cfg = json.loads((root / "config.json").read_text())
        self.tz = ZoneInfo(self.cfg.get("timezone", "America/Detroit"))
        self.key = key if key is not None else os.environ.get("ODDS_API_KEY", "")
        self.http = http
        self._now = now
        self.data = root / "data"
        self.docs = root / "docs" / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.docs.mkdir(parents=True, exist_ok=True)
        self.state = self._load("state.json", {
            "credits_remaining": None, "credits_used": None,
            "last_pull": {}, "close_pulls": {}, "last_scores": {}, "log": []})
        self.flags = self._load("flags.json", {"flags": []})["flags"]

    # clock
    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    def local(self, dt: datetime) -> datetime:
        return dt.astimezone(self.tz)

    # persistence
    def _load(self, name: str, default):
        p = self.data / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError:
                pass
        return default

    def save(self):
        self.state["log"] = self.state.get("log", [])[-60:]
        (self.data / "state.json").write_text(json.dumps(self.state, indent=1))
        (self.data / "flags.json").write_text(json.dumps({"flags": self.flags}, indent=1))

    def note(self, msg: str):
        stamp = self.local(self.now()).strftime("%m-%d %H:%M")
        line = f"{stamp} {msg}"
        print(line)
        self.state.setdefault("log", []).append(line)

    # API
    def get(self, path: str, params: dict | None = None):
        if not self.key:
            raise ApiError(0, "ODDS_API_KEY is not set")
        q = dict(params or {})
        q["apiKey"] = self.key
        url = f"{API}{path}?{urllib.parse.urlencode(q)}"
        status, headers, body = self.http(url)
        rem, used = headers.get("x-requests-remaining"), headers.get("x-requests-used")
        if rem is not None:
            try:
                self.state["credits_remaining"] = int(float(rem))
                self.state["credits_used"] = int(float(used or 0))
            except ValueError:
                pass
        if status != 200:
            raise ApiError(status, body)
        return json.loads(body)


# --------------------------------------------------------------------------- #
# Lines: pull two-way markets out of a book, strip the vig
# --------------------------------------------------------------------------- #
def two_way(book: dict, market: str) -> list[tuple[tuple, int, tuple, int]]:
    """Return [(sideA, priceA, sideB, priceB)] complete pairs for this market.
    A side is (name, point) -- point is None for moneylines."""
    for m in book.get("markets", []):
        if m.get("key") != market:
            continue
        outs = [o for o in m.get("outcomes", []) if isinstance(o.get("price"), (int, float))]
        if market == "h2h":
            if len(outs) != 2:            # three-way (draw) markets are skipped
                return []
            a, b = outs
            return [((a["name"], None), a["price"], (b["name"], None), b["price"])]
        pairs = []
        used = set()
        for i, a in enumerate(outs):
            if i in used or a.get("point") is None:
                continue
            for j in range(i + 1, len(outs)):
                b = outs[j]
                if j in used or b.get("point") is None:
                    continue
                if market == "spreads" and b["name"] != a["name"] and abs(a["point"] + b["point"]) < 1e-9:
                    pass
                elif market == "totals" and {a["name"], b["name"]} == {"Over", "Under"} and a["point"] == b["point"]:
                    pass
                else:
                    continue
                pairs.append(((a["name"], a["point"]), a["price"], (b["name"], b["point"]), b["price"]))
                used.update((i, j))
                break
        return pairs
    return []


def devig(ia: float, ib: float) -> tuple[float, float]:
    """Remove the vig with the power method: find k so ia^k + ib^k = 1.
    Books load most of their margin onto the longshot; splitting it evenly
    (ia / (ia + ib)) overrates underdogs on lopsided moneylines. For lines near
    50/50 the two methods agree."""
    if ia + ib <= 1.0:
        tot = ia + ib
        return ia / tot, ib / tot
    lo, hi = 1.0, 8.0
    for _ in range(60):
        k = (lo + hi) / 2
        if ia ** k + ib ** k > 1.0:
            lo = k
        else:
            hi = k
    k = (lo + hi) / 2
    qa, qb = ia ** k, ib ** k
    tot = qa + qb
    return qa / tot, qb / tot


def novig_table(event: dict, market: str) -> dict:
    """{side: {book: (price, fair_prob_from_that_book)}} for one event+market."""
    table: dict = {}
    for bk in event.get("bookmakers", []):
        for sa, pa, sb, pb in two_way(bk, market):
            ia, ib = implied(pa), implied(pb)
            if ia <= 0 or ib <= 0:
                continue
            qa, qb = devig(ia, ib)
            table.setdefault(sa, {})[bk["key"]] = (pa, qa)
            table.setdefault(sb, {})[bk["key"]] = (pb, qb)
    return table


def fair_prob(cfg: dict, quotes: dict, exclude: str | None = None):
    """Weighted no-vig probability for one side from every book but `exclude`.
    Returns (p, n_books, sharp_books) or None when the market is too thin."""
    w = cfg.get("sharp_weights", {})
    tot_w = acc = 0.0
    books, sharps = 0, []
    for bk, (_, q) in quotes.items():
        if bk == exclude:
            continue
        wt = float(w.get(bk, 1.0))
        acc += wt * q
        tot_w += wt
        books += 1
        if bk in w:
            sharps.append(bk)
    if books < cfg.get("min_books_for_fair", 2):
        return None
    if not sharps and books < cfg.get("min_books_without_sharp", 5):
        return None
    return acc / tot_w, books, sharps


def opposite(market: str, side: tuple, event: dict) -> tuple:
    name, point = side
    if market == "totals":
        return ("Under" if name == "Over" else "Over", point)
    other = event["away_team"] if name == event["home_team"] else event["home_team"]
    return (other, None if point is None else -point)


def scan_event(cfg: dict, event: dict) -> list[dict]:
    """Every (my book, side) price in this event with its edge over fair."""
    mine = set(cfg["my_books"])
    out = []
    for market in ("spreads", "totals", "h2h"):
        table = novig_table(event, market)
        for side, quotes in table.items():
            for bk, (price, _) in quotes.items():
                if bk not in mine:
                    continue
                f = fair_prob(cfg, quotes, exclude=bk)
                if not f:
                    continue
                p, n, sharps = f
                others = sorted(
                    ((b, pr) for b, (pr, _) in quotes.items() if b != bk),
                    key=lambda x: -profit(x[1]))
                out.append({
                    "market": market, "side": side[0], "point": side[1],
                    "book": bk, "price": price, "fair_p": round(p, 4),
                    "fair_odds": fair_american(p), "ev": round(ev(p, price), 4),
                    "n_books": n, "sharps": sharps,
                    "others": [[b, pr] for b, pr in others[:6]],
                })
    return out


def best_per_side(cands: list[dict]) -> list[dict]:
    """One entry per event side -- the best-paying of my books."""
    best: dict = {}
    for c in cands:
        k = (c["market"], c["side"], c["point"])
        if k not in best or profit(c["price"]) > profit(best[k]["price"]):
            best[k] = c
    return list(best.values())


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
def snap_dir(ctx: Ctx, sport: str) -> Path:
    return ctx.data / "snapshots" / sport


def save_snapshot(ctx: Ctx, sport: str, kind: str, events: list) -> Path:
    now = ctx.now()
    d = snap_dir(ctx, sport) / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    p = d / (now.strftime("%H%M%S") + ".json")
    p.write_text(json.dumps({"pulled_at": iso(now), "kind": kind, "events": events},
                            separators=(",", ":")))
    return p


def all_snapshots(ctx: Ctx, sport: str) -> list[Path]:
    d = snap_dir(ctx, sport)
    return sorted(d.glob("*/*.json")) if d.exists() else []


def latest_snapshot(ctx: Ctx, sport: str):
    snaps = all_snapshots(ctx, sport)
    return json.loads(snaps[-1].read_text()) if snaps else None


def prune_snapshots(ctx: Ctx, sport: str, keep_days: int = 10):
    cutoff = (ctx.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    d = snap_dir(ctx, sport)
    if not d.exists():
        return
    for day in d.iterdir():
        if day.is_dir() and day.name < cutoff:
            for f in day.glob("*.json"):
                f.unlink()
            day.rmdir()


# --------------------------------------------------------------------------- #
# Scheduling: when to spend credits
# --------------------------------------------------------------------------- #
def pull_cost(ctx: Ctx, sc: dict) -> int:
    return len(sc["markets"]) * max(1, math.ceil(len(ctx.cfg["books"]) / 10))


def due_pull(ctx: Ctx, sport: str, sc: dict, events: list) -> str | None:
    now = ctx.now()
    upcoming = [e for e in events
                if now + timedelta(minutes=5) < parse_ts(e["commence_time"])
                <= now + timedelta(hours=sc["lookahead_hours"])]
    if not upcoming:
        return None
    last = ctx.state["last_pull"].get(sport)
    since = (now - parse_ts(last)) if last else timedelta(days=999)
    soon = [e for e in upcoming
            if parse_ts(e["commence_time"]) - now <= timedelta(minutes=sc["close_window_min"])]
    today = ctx.local(now).strftime("%Y-%m-%d")
    closes = ctx.state["close_pulls"].get(sport, {}).get(today, 0)
    if soon and since >= timedelta(minutes=45) and closes < sc["max_close_pulls_per_day"]:
        return "close"
    if ctx.local(now).hour in sc["routine_hours"] and since >= timedelta(hours=3):
        return "routine"
    if last is None:
        return "routine"
    return None


def can_spend(ctx: Ctx, cost: int, kind: str) -> bool:
    rem = ctx.state.get("credits_remaining")
    if rem is None:
        return True
    floor = ctx.cfg["credit_reserve"] if kind == "routine" else 5
    return rem - cost >= floor


# --------------------------------------------------------------------------- #
# Flag log: first sighting, closing line, result
# --------------------------------------------------------------------------- #
def flag_id(event_id: str, c: dict) -> str:
    return f"{event_id}|{c['market']}|{c['side']}"


def log_flags(ctx: Ctx, sport: str, snap: dict) -> int:
    pulled = parse_ts(snap["pulled_at"])
    known = {f["id"]: f for f in ctx.flags}
    lo, hi = ctx.cfg["min_ev"], ctx.cfg["max_ev"]
    new = 0
    for e in snap["events"]:
        if parse_ts(e["commence_time"]) <= pulled + timedelta(minutes=5):
            continue
        for c in best_per_side(scan_event(ctx.cfg, e)):
            fid = flag_id(e["id"], c)
            if fid in known:
                f = known[fid]
                if f["status"] == "open" and c["point"] == f["point"]:
                    f["last_seen"], f["last_ev"] = snap["pulled_at"], c["ev"]
                    f["peak_ev"] = max(f.get("peak_ev", f["ev"]), c["ev"])
                continue
            if not (lo <= c["ev"] <= hi):
                continue
            f = dict(c)
            f.update({
                "id": fid, "sport": sport, "event_id": e["id"],
                "home": e["home_team"], "away": e["away_team"],
                "commence": e["commence_time"], "flagged_at": snap["pulled_at"],
                "last_seen": snap["pulled_at"], "last_ev": c["ev"], "peak_ev": c["ev"],
                "status": "open",
            })
            ctx.flags.append(f)
            known[fid] = f
            new += 1
    return new


def close_flags(ctx: Ctx, sport: str) -> int:
    """After a game starts, compare each flag's price with the last line before kickoff."""
    now = ctx.now()
    pending = [f for f in ctx.flags if f["sport"] == sport and f["status"] == "open"
               and parse_ts(f["commence"]) <= now]
    if not pending:
        return 0
    snaps = [json.loads(p.read_text()) for p in all_snapshots(ctx, sport)]
    done = 0
    for f in pending:
        start = parse_ts(f["commence"])
        ev_snap = None
        for s in snaps:
            if parse_ts(s["pulled_at"]) >= start:
                continue
            for e in s["events"]:
                if e["id"] == f["event_id"]:
                    ev_snap = (s["pulled_at"], e)
        f["status"] = "closed"
        if not ev_snap:
            f["close"] = None
            done += 1
            continue
        pulled_at, e = ev_snap
        table = novig_table(e, f["market"])
        side = (f["side"], f["point"])
        close = {"pulled_at": pulled_at,
                 "mins_before": int((start - parse_ts(pulled_at)).total_seconds() // 60),
                 "later": parse_ts(pulled_at) > parse_ts(f["flagged_at"])}
        quotes = table.get(side)
        fp = fair_prob(ctx.cfg, quotes) if quotes else None
        if fp:
            close.update({"point": f["point"], "fair_p": round(fp[0], 4),
                          "fair_odds": fair_american(fp[0]),
                          "clv_ev": round(ev(fp[0], f["price"]), 4)})
            close["beat"] = close["clv_ev"] > 0
        else:
            # The number moved; compare where this side closed.
            pts = [pt for (nm, pt) in table if nm == f["side"] and pt is not None]
            if pts and f["point"] is not None:
                cp = median(pts)
                close["point"] = cp
                if f["market"] == "spreads":
                    close["beat"] = f["point"] > cp
                elif f["side"] == "Over":
                    close["beat"] = f["point"] < cp
                else:
                    close["beat"] = f["point"] > cp
            else:
                close["beat"] = None
        f["close"] = close
        done += 1
    return done


def grade_one(f: dict, scores: dict) -> tuple[str, float]:
    home, away = scores.get(f["home"]), scores.get(f["away"])
    if home is None or away is None:
        raise KeyError("missing score")
    if f["market"] == "totals":
        total = home + away
        diff = (total - f["point"]) if f["side"] == "Over" else (f["point"] - total)
    else:
        mine = home if f["side"] == f["home"] else away
        theirs = away if f["side"] == f["home"] else home
        diff = mine - theirs + (f["point"] or 0)
    if abs(diff) < 1e-9:
        return "P", 0.0
    if diff > 0:
        return "W", round(profit(f["price"]), 4)
    return "L", -1.0


def grade_flags(ctx: Ctx, sport: str, sc: dict) -> int:
    now = ctx.now()
    wait = timedelta(hours=sc.get("grade_after_hours", 4))
    todo = [f for f in ctx.flags if f["sport"] == sport and f["status"] in ("open", "closed")
            and parse_ts(f["commence"]) + wait <= now]
    if not todo:
        return 0
    last = ctx.state["last_scores"].get(sport)
    if last and now - parse_ts(last) < timedelta(hours=ctx.cfg.get("grade_every_hours", 18)):
        return 0
    if not can_spend(ctx, 2, "grade"):
        ctx.note(f"{sport}: skipped grading, credits low")
        return 0
    data = ctx.get(f"/sports/{sc['key']}/scores", {"daysFrom": 3, "dateFormat": "iso"})
    ctx.state["last_scores"][sport] = iso(now)
    by_id = {g["id"]: g for g in data}
    graded = 0
    for f in todo:
        g = by_id.get(f["event_id"])
        if g and g.get("completed") and g.get("scores"):
            try:
                sc_map = {s["name"]: float(s["score"]) for s in g["scores"]}
                f["result"], f["units"] = grade_one(f, sc_map)
                f["final"] = f"{f['away']} {sc_map[f['away']]:g} @ {f['home']} {sc_map[f['home']]:g}"
                f["status"] = "graded"
                graded += 1
            except (KeyError, ValueError, TypeError):
                pass
        elif now - parse_ts(f["commence"]) > timedelta(days=3, hours=12):
            f["status"], f["result"], f["units"] = "void", "V", 0.0
            f["final"] = "no final score found"
    return graded


# --------------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------------- #
def run(ctx: Ctx) -> int:
    errors = []
    for sport, sc in ctx.cfg["sports"].items():
        if not sc.get("enabled"):
            continue
        try:
            events = ctx.get(f"/sports/{sc['key']}/events", {"dateFormat": "iso"})
            kind = due_pull(ctx, sport, sc, events)
            if kind:
                cost = pull_cost(ctx, sc)
                if can_spend(ctx, cost, kind):
                    odds = ctx.get(f"/sports/{sc['key']}/odds", {
                        "bookmakers": ",".join(ctx.cfg["books"]),
                        "markets": ",".join(sc["markets"]),
                        "oddsFormat": "american", "dateFormat": "iso"})
                    save_snapshot(ctx, sport, kind, odds)
                    ctx.state["last_pull"][sport] = iso(ctx.now())
                    if kind == "close":
                        day = ctx.local(ctx.now()).strftime("%Y-%m-%d")
                        cp = ctx.state["close_pulls"].setdefault(sport, {})
                        cp[day] = cp.get(day, 0) + 1
                        for d in [d for d in cp if d < day]:
                            del cp[d]
                    new = log_flags(ctx, sport, latest_snapshot(ctx, sport))
                    ctx.note(f"{sport}: {kind} pull, {len(odds)} games, {new} new flag(s), "
                             f"{ctx.state['credits_remaining']} credits left")
                else:
                    ctx.note(f"{sport}: {kind} pull skipped to protect credits "
                             f"({ctx.state['credits_remaining']} left)")
            closed = close_flags(ctx, sport)
            graded = grade_flags(ctx, sport, sc)
            if closed or graded:
                ctx.note(f"{sport}: {closed} closing line(s) recorded, {graded} graded")
            prune_snapshots(ctx, sport)
        except ApiError as e:
            msg = f"{sport}: {e}"
            if e.status == 401:
                msg += " -- check the ODDS_API_KEY secret, or credits may be used up for the month"
            ctx.note(msg)
            errors.append(msg)
        except (OSError, ValueError, KeyError) as e:
            ctx.note(f"{sport}: {type(e).__name__}: {e}")
            errors.append(f"{sport}: {e}")
    ctx.state["last_run"] = iso(ctx.now())
    ctx.state["last_errors"] = errors
    ctx.save()
    build_site(ctx)
    return 1 if errors and len(errors) == sum(1 for s in ctx.cfg["sports"].values() if s.get("enabled")) else 0


def check(ctx: Ctx) -> int:
    """First run: prove the key works and see which books actually come back."""
    sports = ctx.get("/sports")
    active = {s["key"] for s in sports if s.get("active")}
    print(f"Key works. Credits remaining: {ctx.state['credits_remaining']}")
    target = next((sc for sc in ctx.cfg["sports"].values() if sc["key"] in active), None)
    if not target:
        print("None of the configured sports are in season right now.")
        ctx.save()
        return 0
    odds = ctx.get(f"/sports/{target['key']}/odds", {
        "bookmakers": ",".join(ctx.cfg["books"]), "markets": "h2h",
        "oddsFormat": "american", "dateFormat": "iso"})
    seen: dict = {}
    for e in odds:
        for b in e.get("bookmakers", []):
            seen[b["key"]] = seen.get(b["key"], 0) + 1
    print(f"{target['label']}: {len(odds)} games on the board")
    for b in ctx.cfg["books"]:
        name = ctx.cfg["book_names"].get(b, b)
        print(f"  {name:14s} {'priced ' + str(seen[b]) + ' games' if b in seen else 'NOT RETURNED'}")
    print(f"Credits remaining after check: {ctx.state['credits_remaining']}")
    ctx.state["check"] = {"at": iso(ctx.now()), "sport": target["label"],
                          "books": {b: seen.get(b, 0) for b in ctx.cfg["books"]}}
    ctx.save()
    build_site(ctx)
    return 0


# --------------------------------------------------------------------------- #
# Site data
# --------------------------------------------------------------------------- #
def tally(fs: list[dict]) -> dict:
    w = sum(1 for f in fs if f.get("result") == "W")
    l = sum(1 for f in fs if f.get("result") == "L")
    p = sum(1 for f in fs if f.get("result") == "P")
    units = round(sum(f.get("units", 0.0) for f in fs), 2)
    risked = w + l
    clv = [f["close"] for f in fs if f.get("close") and f["close"].get("later")]
    beats = [c["beat"] for c in clv if c.get("beat") is not None]
    clv_evs = [c["clv_ev"] for c in clv if "clv_ev" in c]
    exp = [f["fair_p"] for f in fs if f.get("result") in ("W", "L")]
    return {
        "n": w + l + p, "w": w, "l": l, "p": p,
        "win_pct": round(w / risked, 4) if risked else None,
        "expected_pct": round(sum(exp) / len(exp), 4) if exp else None,
        "units": units, "roi": round(units / risked, 4) if risked else None,
        "avg_ev": round(sum(f["ev"] for f in fs) / len(fs), 4) if fs else None,
        "clv_n": len(beats),
        "beat_close_pct": round(sum(beats) / len(beats), 4) if beats else None,
        "avg_clv": round(sum(clv_evs) / len(clv_evs), 4) if clv_evs else None,
    }


def view(ctx: Ctx, f: dict) -> dict:
    keep = ("id", "sport", "home", "away", "commence", "market", "side", "point", "book",
            "price", "fair_p", "fair_odds", "ev", "n_books", "sharps", "flagged_at",
            "last_ev", "status", "result", "units", "final", "close", "others")
    out = {k: f.get(k) for k in keep}
    out["book_name"] = ctx.cfg["book_names"].get(f.get("book"), f.get("book"))
    return out


def build_site(ctx: Ctx):
    now = ctx.now()
    board = {"generated": iso(now), "credits": {
                 "remaining": ctx.state.get("credits_remaining"),
                 "used": ctx.state.get("credits_used")},
             "min_ev": ctx.cfg["min_ev"], "book_names": ctx.cfg["book_names"],
             "my_books": ctx.cfg["my_books"],
             "errors": ctx.state.get("last_errors", []),
             "log": ctx.state.get("log", [])[-12:],
             "check": ctx.state.get("check"), "sports": {}}
    logged = {f["id"]: f for f in ctx.flags}
    for sport, sc in ctx.cfg["sports"].items():
        if not sc.get("enabled"):
            continue
        snap = latest_snapshot(ctx, sport)
        entry = {"label": sc["label"], "last_pull": ctx.state["last_pull"].get(sport),
                 "games": 0, "flags": [], "watch": []}
        if snap:
            live = [e for e in snap["events"] if parse_ts(e["commence_time"]) > now]
            entry["games"] = len(live)
            for e in live:
                for c in best_per_side(scan_event(ctx.cfg, e)):
                    if c["ev"] < ctx.cfg["watch_ev"] or c["ev"] > ctx.cfg["max_ev"]:
                        continue
                    row = dict(c, home=e["home_team"], away=e["away_team"],
                               commence=e["commence_time"])
                    lg = logged.get(flag_id(e["id"], c))
                    if lg:
                        row["logged"] = {"price": lg["price"], "book": lg["book"],
                                         "point": lg["point"], "at": lg["flagged_at"]}
                    (entry["flags"] if c["ev"] >= ctx.cfg["min_ev"] else entry["watch"]).append(row)
            entry["flags"].sort(key=lambda r: -r["ev"])
            entry["watch"].sort(key=lambda r: -r["ev"])
            entry["watch"] = entry["watch"][:10]
        board["sports"][sport] = entry
    (ctx.docs / "board.json").write_text(json.dumps(board, indent=1))

    done = [f for f in ctx.flags if f["status"] == "graded"]
    buckets = {"2.5–4%": (0.025, 0.04), "4–6%": (0.04, 0.06), "6%+": (0.06, 9)}
    record = {
        "generated": iso(now),
        "overall": tally(done),
        "by_sport": {ctx.cfg["sports"][s]["label"]: tally([f for f in done if f["sport"] == s])
                     for s in ctx.cfg["sports"] if any(f["sport"] == s for f in done)},
        "by_market": {m: tally([f for f in done if f["market"] == m])
                      for m in ("spreads", "totals", "h2h") if any(f["market"] == m for f in done)},
        "by_ev": {k: tally([f for f in done if lo <= f["ev"] < hi])
                  for k, (lo, hi) in buckets.items() if any(lo <= f["ev"] < hi for f in done)},
        "pending": [view(ctx, f) for f in sorted(
            (f for f in ctx.flags if f["status"] in ("open", "closed")),
            key=lambda f: f["commence"])],
        "recent": [view(ctx, f) for f in sorted(
            done + [f for f in ctx.flags if f["status"] == "void"],
            key=lambda f: f["commence"], reverse=True)[:80]],
    }
    (ctx.docs / "record.json").write_text(json.dumps(record, indent=1))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "check", "site"])
    a = ap.parse_args(argv)
    ctx = Ctx()
    try:
        if a.cmd == "run":
            return run(ctx)
        if a.cmd == "check":
            return check(ctx)
        build_site(ctx)
        return 0
    except ApiError as e:
        print(f"API error: {e}")
        if e.status in (0, 401):
            print("Add your key as the ODDS_API_KEY repository secret (Settings > Secrets and variables > Actions).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
