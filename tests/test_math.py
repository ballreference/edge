import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import edge
f = lambda **k: dict(dict(home="H", away="A", price=-110, point=None), **k)
S = {"H": 24, "A": 21}
assert edge.grade_one(f(market="spreads", side="H", point=-3), S)[0] == "P"      # won by exactly 3
assert edge.grade_one(f(market="spreads", side="H", point=-2.5), S)[0] == "W"
assert edge.grade_one(f(market="spreads", side="A", point=3.5), S)[0] == "W"      # dog +3.5 covers
assert edge.grade_one(f(market="spreads", side="A", point=2.5), S)[0] == "L"
assert edge.grade_one(f(market="totals", side="Over", point=45), S)[0] == "P"
assert edge.grade_one(f(market="totals", side="Under", point=45.5), S)[0] == "W"
assert edge.grade_one(f(market="h2h", side="A", price=150), S) == ("L", -1.0)
assert edge.grade_one(f(market="h2h", side="H", price=-150), S) == ("W", round(100/150, 4))
assert abs(edge.implied(-110) - 0.52381) < 1e-4 and abs(edge.implied(150) - 0.4) < 1e-9
assert edge.fair_american(0.5) == -100 and edge.fair_american(0.6) == -150 and edge.fair_american(0.4) == 150
assert abs(edge.ev(0.5, 100)) < 1e-9 and abs(edge.ev(0.5238095, -110)) < 1e-6
# -110/-110 no-vig is 50/50; a +105 elsewhere on the same side is ~+2.5% EV
ev = {"id": "g", "home_team": "H", "away_team": "A", "commence_time": "2026-10-04T17:00:00Z", "bookmakers": [
  {"key": "pinnacle", "markets": [{"key": "spreads", "outcomes": [{"name": "H", "price": -108, "point": -3}, {"name": "A", "price": -108, "point": 3}]}]},
  {"key": "lowvig", "markets": [{"key": "spreads", "outcomes": [{"name": "H", "price": -105, "point": -3}, {"name": "A", "price": -105, "point": 3}]}]},
  {"key": "fanduel", "markets": [{"key": "spreads", "outcomes": [{"name": "H", "price": -125, "point": -3}, {"name": "A", "price": 105, "point": 3}]}]},
  {"key": "draftkings", "markets": [{"key": "spreads", "outcomes": [{"name": "H", "price": -110, "point": -3.5}, {"name": "A", "price": -110, "point": 3.5}]}]},
]}
cfg = edge.json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text())
c = {(x["book"], x["side"], x["point"]): x for x in edge.scan_event(cfg, ev)}
fd = c[("fanduel", "A", 3)]
assert abs(fd["fair_p"] - 0.5) < 1e-9 and abs(fd["ev"] - 0.025) < 1e-9, fd
assert ("draftkings", "A", 3.5) not in c     # -3.5 has no other book on that number -> no fair price
print("all math checks pass")
