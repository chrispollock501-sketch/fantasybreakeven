"""
Build understat_goals_{season}.json — the goal-location overlay sync_gameweek.py
optionally consumes to split goals into inside-the-box (80) / outside-the-box (100).

WHY THIS IS A SEPARATE SCRIPT
    understat.com is NOT reachable from the Claude sandbox — its network allowlist
    only permits GitHub. Everything else in this pipeline runs from the sandbox;
    this one script has to run somewhere with ordinary internet access:

      a) Chris's own laptop:      python3 fetch_understat_shots.py
      b) a GitHub Action on a schedule, committing the JSON back to a repo
      c) any small box with cron

    Whichever runner you pick, drop the resulting understat_goals_{season}.json next
    to sync_gameweek.py before running it. If the file isn't there, sync_gameweek.py
    scores every goal at the inside-the-box rate and says so — nothing breaks.

⚠ NOT VERIFIED AGAINST A LIVE UNDERSTAT PAGE
    Written 2026-08-20 from Understat's documented page structure. It could not be
    run end-to-end from the sandbox (no network route) and the Chrome bridge was
    offline at the time. RUN IT ONCE WITH --dry-run AND READ THE OUTPUT before
    trusting it. If Understat has changed its embedded-variable format, the two
    regexes in extract_json() are the only thing that needs updating.

WHAT IT COSTS TO SKIP
    calibration-results.md puts the outside-the-box goal at 2.2% of all points, and
    it is close to position-neutral, so leaving this unrun distorts the price economy
    far less than the 27% gap the Core-Insights switch just closed. Treat it as
    polish, not as a blocker.

USAGE
    python3 fetch_understat_shots.py                     # current season
    python3 fetch_understat_shots.py --season 2025-2026  # backfill / test
    python3 fetch_understat_shots.py --dry-run           # fetch + report, write nothing
    python3 fetch_understat_shots.py --report-unmatched  # list every name it couldn't map
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CI = 'https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/data'
UNDERSTAT = 'https://understat.com'
UA = {'User-Agent': 'Mozilla/5.0 (compatible; pl-fantasy-breakeven/1.0)'}

# Penalty area, in Understat's normalised pitch coordinates (X along the length of
# the pitch toward the goal being attacked, Y across it, both 0-1).
#   18-yard box: 16.5m deep on a 105m pitch, 40.32m wide on a 68m pitch.
BOX_X = 1 - 16.5 / 105          # 0.8429
BOX_Y_LO = (68 - 40.32) / 2 / 68  # 0.2035
BOX_Y_HI = 1 - BOX_Y_LO           # 0.7965

# Understat's club names -> the club's FPL short_name. Only the ones that don't
# fall out of a plain normalised match are listed.
TEAM_ALIASES = {
    'manchester united': 'MUN', 'manchester city': 'MCI', 'newcastle united': 'NEW',
    'wolverhampton wanderers': 'WOL', 'tottenham': 'TOT', 'west ham': 'WHU',
    'leicester': 'LEI', 'brighton': 'BHA', 'nottingham forest': 'NFO',
    'sheffield united': 'SHU', 'leeds': 'LEE', 'leeds united': 'LEE',
    'ipswich': 'IPS', 'ipswich town': 'IPS', 'hull city': 'HUL', 'hull': 'HUL',
    'coventry': 'COV', 'coventry city': 'COV', 'crystal palace': 'CRY',
    'aston villa': 'AVL', 'bournemouth': 'BOU', 'brentford': 'BRE',
    'nottingham': 'NFO', 'luton': 'LUT', 'burnley': 'BUR', 'sunderland': 'SUN',
}


def norm(s):
    """Lowercase, strip accents and punctuation — the cheap half of name matching."""
    s = unicodedata.normalize('NFKD', str(s or ''))
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z ]', '', s.lower()).strip()


# ------------------------------------------------------------------------ fetching

def get(url, timeout=30, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f'could not fetch {url}: {last}')


def extract_json(html, var_name):
    """Understat embeds its data as  var X = JSON.parse('<hex-escaped json>');
    Two spellings are seen in the wild, hence two patterns. If both miss, Understat
    has changed format — that's the one thing to fix in this file."""
    for pat in (var_name + r"\s*=\s*JSON\.parse\('((?:[^'\\]|\\.)*)'\)",
                var_name + r"\s*=\s*JSON\.parse\(\"((?:[^\"\\]|\\.)*)\"\)"):
        m = re.search(pat, html)
        if m:
            raw = m.group(1).encode('utf-8').decode('unicode_escape')
            return json.loads(raw)
    return None


def ci_url(season, *parts):
    return CI + '/' + season + '/' + '/'.join(urllib.parse.quote(p) for p in parts)


def fetch_csv(url):
    return list(csv.DictReader(io.StringIO(get(url))))


# ------------------------------------------------------------------- reference data

def build_reference(season):
    """Return (name_index, gw_index).

    name_index: (normalised name, club short_name) -> FPL player_code, plus a
                club-agnostic fallback for names unique across the league.
    gw_index:   (date 'YYYY-MM-DD', home short, away short) -> gameweek
    """
    teams = fetch_csv(ci_url(season, 'teams.csv'))
    code_to_short = {t['code']: t['short_name'] for t in teams}

    players = fetch_csv(ci_url(season, 'players.csv'))
    name_index, name_counts = {}, {}
    for p in players:
        try:
            code = int(p['player_code'])
        except (KeyError, ValueError):
            continue
        club = code_to_short.get(p.get('team_code'), '')
        full = norm(f"{p.get('first_name','')} {p.get('second_name','')}")
        web = norm(p.get('web_name', ''))
        second = norm(p.get('second_name', ''))
        for n in {full, web, second}:
            if not n:
                continue
            name_index[(n, club)] = code
            name_counts.setdefault(n, set()).add(code)
    # club-agnostic fallback, only where the name is unambiguous league-wide
    for n, codes in name_counts.items():
        if len(codes) == 1:
            name_index.setdefault((n, ''), next(iter(codes)))

    gw_index = {}
    for gw in range(1, 39):
        rows = None
        try:
            rows = fetch_csv(ci_url(season, 'By Tournament', 'Premier League',
                                    f'GW{gw}', 'matches.csv'))
        except Exception:  # noqa: BLE001
            continue
        for m in rows or []:
            # matches.csv identifies clubs by FPL team *code*, not team id — those are
            # different numbering systems, and using the wrong one here silently
            # produced an empty gw_index and therefore an empty overlay file.
            try:
                h = code_to_short[str(int(float(m['home_team'])))]
                a = code_to_short[str(int(float(m['away_team'])))]
                d = (m.get('kickoff_time') or '')[:10]
            except (KeyError, ValueError, TypeError):
                continue
            if d:
                gw_index[(d, h, a)] = gw
    return name_index, gw_index


def understat_year(season):
    """'2026-2027' -> 2026 (Understat labels a season by its starting year)."""
    return int(str(season).split('-')[0])


def detect_season(candidates=('2026-2027', '2025-2026')):
    """Same current-season-first fallback sync_gameweek.py uses, so the overlay file
    this writes always carries the season name the sync will look for."""
    for s in candidates:
        try:
            get(ci_url(s, 'players.csv'), timeout=15, retries=1)
            return s
        except Exception:  # noqa: BLE001
            continue
    return None


def resolve_team(name):
    n = norm(name)
    if n in TEAM_ALIASES:
        return TEAM_ALIASES[n]
    return None, n  # caller falls back to fuzzy matching on short names


# ------------------------------------------------------------------------- main job

def classify(shot):
    """-> 'penalty' | 'inside' | 'outside' | None (not a goal)."""
    if shot.get('result') != 'Goal':
        return None
    if (shot.get('situation') or '') == 'Penalty':
        return 'penalty'
    try:
        x, y = float(shot['X']), float(shot['Y'])
    except (KeyError, TypeError, ValueError):
        return 'inside'  # no coordinates -> default to the common case
    return 'inside' if (x >= BOX_X and BOX_Y_LO <= y <= BOX_Y_HI) else 'outside'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--season', default=None,
                    help='e.g. 2026-2027. Default: auto-detect, matching sync_gameweek.py')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--report-unmatched', action='store_true')
    ap.add_argument('--cache', default=os.path.join(ROOT, '.understat_cache'))
    a = ap.parse_args()

    season = a.season or detect_season()
    if season is None:
        print('Could not reach FPL-Core-Insights for any season. Nothing written.',
              file=sys.stderr)
        return 2
    year = understat_year(season)
    os.makedirs(a.cache, exist_ok=True)

    print(f'Reference data for {season} from FPL-Core-Insights…')
    name_index, gw_index = build_reference(season)
    all_shorts = {k[1] for k in gw_index} | {k[2] for k in gw_index}
    print(f'  {len(name_index)} name keys · {len(gw_index)} fixtures with a gameweek')

    print(f'Fetching Understat EPL {year} fixture list…')
    league_html = get(f'{UNDERSTAT}/league/EPL/{year}')
    dates = extract_json(league_html, 'datesData')
    if not dates:
        print('\n!! Could not find datesData on the Understat league page.\n'
              '   Understat has changed its page format — fix extract_json() and\n'
              '   re-run. Nothing was written.', file=sys.stderr)
        return 2
    finished = [m for m in dates if m.get('isResult')]
    print(f'  {len(finished)} finished matches of {len(dates)}')

    def to_short(name):
        r = resolve_team(name)
        if isinstance(r, str):
            return r
        n = r[1]
        for s in all_shorts:
            if norm(s) == n[:3] or n.startswith(norm(s)):
                return s
        # last resort: first three letters uppercased
        return n[:3].upper()

    goals = {}
    unmatched_names, unmatched_fixtures = {}, []
    for i, m in enumerate(finished, 1):
        mid = str(m['id'])
        cache_file = os.path.join(a.cache, f'{year}_{mid}.json')
        if os.path.exists(cache_file):
            with open(cache_file) as f:
                shots = json.load(f)
        else:
            html = get(f'{UNDERSTAT}/match/{mid}')
            shots = extract_json(html, 'shotsData')
            if shots is None:
                print(f'  match {mid}: no shotsData found, skipped', file=sys.stderr)
                continue
            with open(cache_file, 'w') as f:
                json.dump(shots, f)
            time.sleep(0.7)  # be a polite scraper
        if i % 20 == 0:
            print(f'  {i}/{len(finished)} matches')

        h = to_short((m.get('h') or {}).get('title', ''))
        aw = to_short((m.get('a') or {}).get('title', ''))
        date = (m.get('datetime') or '')[:10]
        gw = gw_index.get((date, h, aw))
        if gw is None:
            # kickoff dates can differ by a day across timezones, so fall back to
            # matching on the fixture pairing alone (unique within a season).
            for (_d, hh, aa), g in gw_index.items():
                if hh == h and aa == aw:
                    gw = g
                    break
        if gw is None:
            unmatched_fixtures.append((date, h, aw))
            continue

        for side in ('h', 'a'):
            club = h if side == 'h' else aw
            for shot in shots.get(side, []):
                kind = classify(shot)
                if kind in (None, 'penalty'):
                    continue  # penalties already score at the inside rate
                pname = norm(shot.get('player', ''))
                code = name_index.get((pname, club)) or name_index.get((pname, ''))
                if code is None:
                    unmatched_names[(shot.get('player', ''), club)] = \
                        unmatched_names.get((shot.get('player', ''), club), 0) + 1
                    continue
                bucket = goals.setdefault(str(code), {}).setdefault(str(gw),
                                                                    {'inside': 0, 'outside': 0})
                bucket[kind] += 1

    tot_in = sum(v['inside'] for p in goals.values() for v in p.values())
    tot_out = sum(v['outside'] for p in goals.values() for v in p.values())
    print(f'\n{len(goals)} players · {tot_in} inside-box goals · {tot_out} outside-box goals')
    if tot_in + tot_out:
        print(f'  outside-the-box share {tot_out / (tot_in + tot_out) * 100:.1f}% '
              f'(historically ~12-15% of open-play goals — a wildly different number '
              f'here means the coordinate test or the parser is wrong)')
    if unmatched_fixtures:
        print(f'  {len(unmatched_fixtures)} fixtures could not be mapped to a gameweek')
    if unmatched_names:
        n = sum(unmatched_names.values())
        print(f'  {n} goals by {len(unmatched_names)} unmatched player names '
              f'(these silently score at the inside rate)')
        if a.report_unmatched:
            for (name, club), c in sorted(unmatched_names.items(), key=lambda x: -x[1]):
                print(f'      {name} ({club}) × {c}')

    if a.dry_run:
        print('\n--dry-run: nothing written.')
        return 0

    out = os.path.join(ROOT, f'understat_goals_{season}.json')
    with open(out, 'w') as f:
        json.dump(goals, f, indent=1, sort_keys=True)
    print(f'\nWrote {out}. Put it next to sync_gameweek.py and re-run the sync.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
