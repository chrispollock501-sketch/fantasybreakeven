# pl-fantasy-sync

Daily automated scoring for the Premier League breakeven fantasy site. Once this is
set up you never run anything by hand again — it pulls each gameweek's stats, scores
them, writes them to Supabase and recomputes every manager's points, twice a day,
whether your computer is on or not.

## Setup — about ten minutes, once

### 1. Create the repository

On GitHub: **New repository** → name it `pl-fantasy-sync` → **Private** → Create.

Then **uploading an existing file** → drag in everything from this folder, *including
the `.github` folder*. If GitHub's uploader drops the `.github/workflows/sync.yml`
file (its drag-and-drop sometimes skips folders beginning with a dot), create it by
hand: **Add file → Create new file**, type `.github/workflows/sync.yml` as the
filename, and paste the contents in.

### 2. Add your two secrets

**Settings → Secrets and variables → Actions → New repository secret.** Add these two,
exactly these names:

| Name | Value |
|---|---|
| `SUPABASE_URL` | Supabase → Project Settings → Data API → Project URL. Looks like `https://abcdefgh.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase → Project Settings → API keys → **`service_role` / secret** key |

**The `service_role` key is not the one in your website.** The site uses the
publishable/anon key, which is safe in a browser. The service_role key bypasses every
security rule in your database — it belongs only in GitHub Secrets, and never in
`_app.js`, never in a file you upload to Netlify, never pasted into a chat. GitHub
Secrets are write-only: once saved, not even you can read them back, and they're
masked out of the logs automatically.

If you paste the publishable key by mistake the sync refuses to start and says so. If
you paste something else wrong, Supabase rejects the write and the run fails loudly —
either way it never quietly writes nothing.

### 3. Test it

**Actions** tab → **Sync gameweek points** → **Run workflow** → Run.

Watch it run. A healthy first run looks like:

```
Core-Insights season 2026-2027 · FPL mirror season 2026-27
Push mode -> https://abcdefgh.supabase.co
  gw1: 312 players scored
  fixtures        10 rows upserted
  gw_player_stats 312 rows upserted
  compute_all_gw_points(1)
Done — 312 player-gameweek rows across 1 gameweek(s).
```

Before the season's first matches are played it will correctly say
`No gameweek has published player match stats yet — nothing to do.` That's success,
not failure.

That's it. From then on it runs itself at 08:30 and 18:30 UTC (18:30 and 04:30
Brisbane) every day.

## How it behaves

**It re-syncs the last three gameweeks every run, not just the newest one.** That is
deliberate. It means a failed run repairs itself on the next one instead of leaving a
permanent hole, and it means corrections the data feed makes after the fact — a goal
reclassified, an assist added on Monday — flow through automatically. Every write is
an upsert, so re-running is always safe.

**Timing.** The upstream dataset refreshes at 07:30 and 17:30 UTC. The runs sit an
hour after each so they never race it. Saturday-evening matches appear in Sunday
morning's run; a full weekend is settled by Monday morning.

**If the Understat step fails**, the sync still runs. Understat only supplies the
inside-vs-outside-the-box goal split, worth about 2.2% of all points. A failure there
leaves every goal scoring 80 instead of some scoring 100, and posts a warning on the
run. It is never allowed to stop points being scored.

## Running it by hand when you need to

**Actions → Sync gameweek points → Run workflow** takes two optional inputs:

- **gw** — a single gameweek (`5`) or a range (`1-38`) to sync just that.
- **full_resync** — tick this to rebuild the whole season from scratch. Worth doing
  once after any change to the scoring rules.

## Before each new season

Run this locally once, and read the output:

```bash
python3 sync_gameweek.py --audit
```

It reports, gameweek by gameweek, which stats the feed is actually collecting. Some
stats appear healthy as a season total but were only collected from halfway through —
that happened in 2025/26 with dispossessions and saves-inside-the-box. Anything the
audit flags should come out of `scoring_config_v5.json` before the season is scored,
or it quietly rewards second-half-of-the-season players only.

## Keeping the scoring rules in step

`scoring_config_v5.json` and `scoring.py` here are copies of the ones in the main
engine (`pl_fantasy_engine_*.zip`). **If you change the scoring rules, change them in
both places**, then use `full_resync` to rebuild the season on the new values.

## Things worth knowing

- **GitHub pauses scheduled workflows in a repository with no activity for 60 days.**
  If points stop updating over a long off-season, that's the first thing to check —
  the Actions tab will say so, and one click re-enables it.
- Free GitHub accounts get 2,000 Actions minutes a month on private repositories.
  These runs take a couple of minutes each, so roughly 120 minutes a month.
- The data source is [olbauday/FPL-Core-Insights](https://github.com/olbauday/FPL-Core-Insights).
  If it ever goes away, `sync_gameweek.py`'s docstring lists the fallbacks in order.
