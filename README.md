# Edge

Finds prices at your sportsbooks that pay more than the market's fair price, for NFL, NBA and MLB
spreads, totals and moneylines. Logs every flag, records the closing line, and grades it, so the
record shows whether it's really beating the market.

Runs free: GitHub Actions + GitHub Pages on a public repo, and The Odds API free tier (500 credits/month).

## Setup (one time)

1. **Odds API key.** Sign up at https://the-odds-api.com (free Starter plan) and copy the key from the email.
2. **Add the key to this repo.** Settings → Secrets and variables → Actions → New repository secret.
   Name: `ODDS_API_KEY`. Value: your key.
3. **Turn on the site.** Settings → Pages → Build and deployment → Deploy from a branch → `main`, folder `/docs`.
4. **First check.** Actions → Edge → Run workflow → mode `check`. It uses 1 credit and lists which of
   your books came back. After that it runs by itself every hour.

The site lives at `https://<owner>.github.io/<repo>/`.

## Books

On the free plan the tool flags DraftKings, FanDuel, BetMGM and theScore Bet (formerly ESPN Bet).
Caesars and Fanatics are paid-plan only on The Odds API, and bet365 isn't offered, so check those on the Props page.
Pinnacle, LowVig, BetOnline, BetRivers, Hard Rock and Bovada are pulled only to sharpen the fair price.

## How it decides

- **Fair price:** for each line, every book's price is converted to a no-vig probability, then averaged.
  Pinnacle counts 3×, LowVig and BetOnline 1.5×, retail books 1×. A book is never used to judge itself.
  Lines with no sharp book need at least 5 books.
- **Flag:** your book's price beats fair by 2.5%–15% expected profit. Above 15% is almost always a stale
  or broken line, so it's ignored.
- **Closing line:** the last pull before kickoff. Beating it more than half the time is the early sign
  the flags are real.

All thresholds are in `config.json`.

## Credits

Asking for 10 specific books costs the same as one region, so a pull is 3 credits (spreads, totals, moneylines).
Each sport gets one routine pull a day and one or more pulls about an hour before games start, for the closing line.
Grading costs 2 credits per sport per day. A full NFL + NBA month simulates to about 440 credits.
Routine pulls stop when fewer than 30 credits are left, so the closing-line pulls and grading keep working.

## Files

- `edge.py` — the engine (standard library only)
- `config.json` — books, thresholds, schedule per sport
- `data/` — snapshots (kept 10 days), `flags.json` (every flag ever), `state.json`
- `docs/` — the site: board, record, props calculator
- `tests/` — `test_math.py` and `simulate.py` (a month of fake odds against a fake API)
