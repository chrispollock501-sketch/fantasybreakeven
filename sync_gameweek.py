"""
Pull gameweek results and turn them into SQL that fills `fixtures` and
`gw_player_stats`, then triggers `compute_all_gw_points` for every gameweek touched.

WHERE THE DATA COMES FROM (v2 — rewritten 2026-08-20)
    Primary: olbauday/FPL-Core-Insights on GitHub.
        Fuses the official FPL API with detailed per-player match stats and keys
        everything on FPL player ids. Refreshed twice daily (07:30 and 17:30 UTC).
        This is the same source claude/calibration-results.md calibrated the whole
        scoring system on, which is why scoring.py's derive() already speaks its
        column names verbatim.

        - data/{season}/By Tournament/Premier League/GW{n}/playermatchstats.csv
              per player per match: chances created, touches in the opposition box,
              successful dribbles, accurate crosses, aerial duels won, was fouled,
              fouls committed, dribbled past, shots on target, tackles, interceptions,
              blocks, clearances, recoveries, saves, goals prevented, sweeper actions,
              high claims, goals, assists, penalties scored/missed, minutes.
              "By Tournament/Premier League" deliberately, so cup and European matches
              never leak into league scoring.
        - data/{season}/By Gameweek/GW{n}/player_gameweek_stats.csv
              the FPL-official per-gameweek fields the match feed doesn't carry:
              yellow cards, red cards, own goals, penalties saved, clean sheets,
              goals conceded. Discrete per gameweek, not cumulative.
        - data/{season}/players.csv   player_id -> player_code (the permanent FPL
              code this game uses as its player_id) and position.

    Fixtures table: vaastav/Fantasy-Premier-League's fixtures.csv, so `fixtures.id`
        stays the FPL fixture id the schema comment promises. Core-Insights matches
        are mapped onto those ids by (gameweek, home team, away team) — both mirrors
        use identical FPL team ids.

    Optional overlay: understat_goals_{season}.json, if present in ROOT (see
        fetch_understat_shots.py). Splits goals into inside-the-box / outside-the-box
        / penalty. Without it every goal scores at the inside-the-box rate.

WHAT THIS FIXES
    The previous version read only the vaastav mirror, which carries the official FPL
    fields and nothing else. Measured across all 11,492 player-appearances of the
    2025/26 season, that delivered only 73.1% of the points scoring_config_v5.json
    defines — and unevenly: GK 86.9%, DEF 80.9%, MID 66.0%, FWD 60.8%. Since starting
    prices were modelled on the full stat set, every breakeven was unreachable and the
    shortfall was position-biased, which would have repriced the whole market toward
    defenders. This version scores every line in the config except the inside/outside
    box goal split and hit-woodwork.

STILL NOT SCORED, and why
    - goal_outside_box (100 vs 80) — needs shot coordinates. Supply
      understat_goals_{season}.json to close it; otherwise all goals score 80.
    - hit_woodwork (5) — Core-Insights carries it at team level only, no free
      per-player source. 0.2% of points.
    Everything else in scoring_config_v5.json is now fed real data.

USAGE
    python3 sync_gameweek.py                 # every gameweek with published match stats
    python3 sync_gameweek.py --gw 3
    python3 sync_gameweek.py --gw 1-5
    python3 sync_gameweek.py --season 2025-2026
    python3 sync_gameweek.py --out gw_sync.sql
    python3 sync_gameweek.py --audit         # stat-availability audit, no SQL written
    python3 sync_gameweek.py --push          # write straight to Supabase, no SQL file
    python3 sync_gameweek.py --push --dry-run  # do everything except the writes
    python3 sync_gameweek.py --push --latest 3 # only the 3 most recent gameweeks

    Default: writes a SQL file to paste into the Supabase SQL Editor. Upserts only,
    safe to re-run.

    --push instead sends the same upserts to Supabase's REST API and calls
    compute_all_gw_points() for each gameweek, so nothing has to be pasted. It reads
    two environment variables and will not run without them:

        SUPABASE_URL          e.g. https://xxxxxxxx.supabase.co
        SUPABASE_SERVICE_KEY  the service_role / secret key

    The service key bypasses RLS and must NEVER go anywhere near _app.js or any
    file that ends up in the browser — the publishable/anon key is the one that
    belongs there. In the GitHub Action this comes from repository Secrets, so it
    is never written to disk and never appears in a log.
"""
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from scoring import load_config, score_frame  # noqa: E402

CI = 'https://raw.githubusercontent.com/olbauday/FPL-Core-Insights/main/data'
FPL = 'https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data'

# Core-Insights uses 2026-2027; the vaastav mirror uses 2026-27. Same season, two
# spellings, so they're carried together.
SEASONS = [('2026-2027', '2026-27'), ('2025-2026', '2025-26')]

POS_LONG = {'GKP': 'Goalkeeper', 'GK': 'Goalkeeper', 'DEF': 'Defender',
            'MID': 'Midfielder', 'FWD': 'Forward'}

# Stats the audit checks for silent absence. calibration-results.md section 1 found
# three stats in 2025/26 that were healthy-looking season totals but only started
# being collected mid-season (offsides, dispossessed, saves_inside_box). Run --audit
# every season before trusting the feed.
AUDITED = [
    'chances_created', 'touches_opposition_box', 'successful_dribbles',
    'accurate_crosses', 'aerial_duels_won', 'was_fouled', 'fouls_committed',
    'dribbled_past', 'shots_on_target', 'tackles', 'interceptions', 'blocks',
    'clearances', 'recoveries', 'saves', 'goals_prevented', 'sweeper_actions',
    'high_claim',
    # Not in scoring_config_v5.json — these are the three the 2025/26 audit threw out
    # (offsides badly under-collected, dispossessions from GW25, saves-inside-box from
    # GW26). Kept here as canaries: if a season shows them healthy from GW1 they can be
    # scored again, and if the audit can't flag these it can't flag anything.
    'dispossessed', 'saves_inside_box', 'offsides',
]


# --------------------------------------------------------------------------- fetch

def fetch_csv(url, timeout=25):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode('utf-8'))))


def fetch_csv_or_none(url, timeout=25, only_404=False):
    """only_404=True swallows a genuine "this file doesn't exist" and re-raises
    anything else. Use it anywhere absence is meaningful — "the season hasn't started",
    "that gameweek isn't published" — because treating a 503 or a timeout as absence
    is how a transient blip turns into last season's data being written over this
    season's, or into the wrong gameweeks being synced, with no error anywhere."""
    try:
        return fetch_csv(url, timeout)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        if only_404:
            raise
        return None
    except Exception:  # noqa: BLE001
        if only_404:
            raise
        return None


def ci_url(season, *parts):
    return CI + '/' + season + '/' + '/'.join(urllib.parse.quote(p) for p in parts)


def pick_season(override=None):
    """Return (core_insights_season, fpl_mirror_season). Prefers the current season,
    falls back to the previous one if the current has no players.csv yet."""
    candidates = SEASONS
    if override:
        candidates = [(o, f) for (o, f) in SEASONS if override in (o, f)] or \
                     [(override, override)]
    for ci_season, fpl_season in candidates:
        # only_404: a timeout or a 5xx on the current season must NOT be read as
        # "the season hasn't started" and fall through to last season — that would
        # upsert last season's gameweeks under this season's keys and recompute
        # everyone's points off them, silently.
        if fetch_csv_or_none(ci_url(ci_season, 'players.csv'), timeout=15, only_404=True):
            return ci_season, fpl_season
    raise RuntimeError('Could not reach FPL-Core-Insights for any season')


# ------------------------------------------------------------------------ reference

def load_players(ci_season):
    """player_id (Core-Insights, season-scoped) -> {code, position, web_name}."""
    rows = fetch_csv(ci_url(ci_season, 'players.csv'))
    out = {}
    for r in rows:
        try:
            out[r['player_id']] = {
                'code': int(r['player_code']),
                'position': r['position'],
                'web_name': r.get('web_name', ''),
                'team_code': r.get('team_code', ''),
            }
        except (KeyError, ValueError):
            continue
    return out


def load_teams(ci_season):
    """Core-Insights team id -> short_name, and team code -> short_name."""
    rows = fetch_csv(ci_url(ci_season, 'teams.csv'))
    by_id, by_code = {}, {}
    for r in rows:
        by_id[str(int(float(r['id'])))] = r['short_name']
        by_code[r['code']] = r['short_name']
    return by_id, by_code


def load_fpl_fixtures(fpl_season):
    """FPL fixtures, plus a (gw, home short_name, away short_name) -> fixture id index.

    Keyed on club short names deliberately. The vaastav mirror's fixtures.csv
    identifies clubs by FPL *team id* (1-20), while Core-Insights' matches.csv
    identifies them by FPL *team code* (3, 91, 17…). Those are different numbering
    systems and joining them directly silently matches nothing — short names are the
    one identifier both mirrors agree on."""
    teams = fetch_csv_or_none(f'{FPL}/{fpl_season}/teams.csv')
    id_to_short = {str(int(float(t['id']))): t['short_name'] for t in (teams or [])}

    rows = fetch_csv_or_none(f'{FPL}/{fpl_season}/fixtures.csv')
    if rows is None:
        return [], {}, id_to_short
    fixtures, index = [], {}
    for r in rows:
        try:
            fid = int(r['id'])
            gw = int(r['event']) if r.get('event') else None
            home = id_to_short.get(str(int(float(r['team_h']))))
            away = id_to_short.get(str(int(float(r['team_a']))))
        except (KeyError, ValueError, TypeError):
            continue
        f = {
            'id': fid, 'gw': gw, 'home': home, 'away': away,
            'kickoff_time': r.get('kickoff_time') or None,
            'home_score': r.get('team_h_score') or None,
            'away_score': r.get('team_a_score') or None,
            'finished': (r.get('finished') or '').strip() == 'True',
        }
        # fixtures.home_club / away_club are NOT NULL. A fixture whose clubs couldn't
        # be resolved (teams.csv unreachable) must not go into the payload at all —
        # otherwise one missing file turns into a 400 that aborts the push before any
        # points are written, which is the opposite of the "points are unaffected"
        # this degradation is supposed to provide.
        if not (home and away):
            continue
        fixtures.append(f)
        if gw is not None:
            index[(gw, home, away)] = fid
    return fixtures, index, id_to_short


# ------------------------------------------------------------------------- per-gw

def num(v):
    if v in (None, '', 'None'):
        return 0
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return 0


MATCH_SUM_KEYS = [
    'minutes_played', 'goals', 'assists', 'penalties_scored', 'penalties_missed',
    'shots_on_target', 'chances_created', 'touches_opposition_box',
    'successful_dribbles', 'accurate_crosses', 'was_fouled', 'fouls_committed',
    'dribbled_past', 'aerial_duels_won', 'ground_duels_won', 'tackles',
    'interceptions', 'blocks', 'clearances', 'recoveries', 'saves',
    'goals_prevented', 'sweeper_actions', 'high_claim', 'final_third_passes',
]

FPL_KEYS = {
    'clean_sheets': 'clean_sheets',
    'goals_conceded_fpl': 'goals_conceded',
    'penalties_saved': 'penalties_saved',
    'yellow_cards': 'yellow_cards',
    'red_cards': 'red_cards',
    'own_goals': 'own_goals',
}


def fetch_gw(ci_season, gw):
    """Return (playermatchstats rows, player_gameweek_stats rows) or (None, None)."""
    pms = fetch_csv_or_none(
        ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}', 'playermatchstats.csv'))
    pgs = fetch_csv_or_none(
        ci_url(ci_season, 'By Gameweek', f'GW{gw}', 'player_gameweek_stats.csv'))
    return pms, pgs


def build_rows(gw, pms, pgs, players, team_by_id, team_by_code, fixture_index,
               ci_matches, understat):
    """Merge the match feed with the FPL per-gameweek feed into one row per player,
    in the raw column vocabulary scoring.py's derive() expects."""
    # Core match_id -> FPL fixture id.
    # matches.csv's home_team/away_team hold FPL team *codes*, not team ids, so they
    # are resolved to short names before being joined against the fixture index.
    match_to_fixture = {}
    for m in ci_matches or []:
        try:
            h = team_by_code.get(str(int(float(m['home_team']))))
            a = team_by_code.get(str(int(float(m['away_team']))))
            g = int(float(m['gameweek']))
        except (KeyError, ValueError, TypeError):
            continue
        if not (h and a):
            continue
        fid = fixture_index.get((g, h, a)) or fixture_index.get((g, a, h))
        if fid is not None:
            match_to_fixture[m['match_id']] = fid

    by_code = {}
    unmatched = 0
    for r in pms or []:
        meta = players.get(r.get('player_id'))
        if meta is None:
            unmatched += 1
            continue
        code = meta['code']
        row = by_code.get(code)
        if row is None:
            row = {
                'player_id': code,
                'web_name': meta['web_name'],
                'team': team_by_code.get(meta['team_code'], ''),
                'position': meta['position'],
                'gw': gw,
                'match_id': match_to_fixture.get(r.get('match_id')),
                '_matches': 0,
            }
            for k in MATCH_SUM_KEYS:
                row[k] = 0
            by_code[code] = row
        # A genuine double gameweek gives a player two match rows; summing the
        # counting stats is the correct scoring behaviour (feasibility doc 5.4).
        # The schema can only hold one fixture_id — a known cosmetic gap.
        row['_matches'] += 1
        for k in MATCH_SUM_KEYS:
            row[k] += num(r.get(k))
        if row['match_id'] is None:
            row['match_id'] = match_to_fixture.get(r.get('match_id'))

    # FPL-official per-gameweek fields, keyed on the same Core-Insights player_id.
    fpl_by_code = {}
    for r in pgs or []:
        meta = players.get(r.get('id'))
        if meta is None:
            continue
        fpl_by_code[meta['code']] = r

    out = []
    for code, row in by_code.items():
        f = fpl_by_code.get(code)
        for target, src in FPL_KEYS.items():
            row[target] = num(f.get(src)) if f else 0
        row['penalties_missed_fpl'] = 0     # taken from the match feed instead
        row['starts'] = num(f.get('starts')) > 0 if f else row['minutes_played'] >= 60

        # goal location: Understat overlay if we have it, otherwise every goal
        # scores at the inside-the-box rate, which is what the config's
        # goal_inside_box value is.
        pens = row['penalties_scored']
        open_play = max(0, row['goals'] - pens)
        split = (understat or {}).get(str(code), {}).get(str(gw))
        if split:
            inside = min(open_play, num(split.get('inside')))
            outside = min(open_play - inside, num(split.get('outside')))
            leftover = open_play - inside - outside
            row['goal_inside_box'] = inside + leftover
            row['goal_outside_box'] = outside
        else:
            row['goal_inside_box'] = open_play
            row['goal_outside_box'] = 0
        row['goal_penalty'] = pens
        out.append(row)

    return out, unmatched


# ---------------------------------------------------------------------------- sql

def sql_str(s):
    return "'" + str(s).replace("'", "''") + "'" if s is not None else 'null'


# --------------------------------------------------------------------------- push

class Supabase:
    """Thin PostgREST client. Deliberately stdlib-only — this runs in a GitHub
    Action where every extra dependency is another thing that can break a scheduled
    job at 6am on a Sunday."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        """urllib re-sends Authorization headers across a redirect, so a mistyped
        SUPABASE_URL could hand the service key to whatever the redirect points at —
        and turn the POST into a GET, so it would report success having written
        nothing. Redirects are simply refused."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise RuntimeError(
                f'SUPABASE_URL redirected ({code} -> {newurl}). Refusing to follow: '
                f'a redirect would forward the service key. Check SUPABASE_URL is the '
                f'exact https://<project>.supabase.co with no trailing path.')

    def __init__(self, url, key, dry_run=False):
        self.url = url.rstrip('/')
        if not self.url.startswith('https://'):
            raise RuntimeError('SUPABASE_URL must start with https:// — refusing to '
                               'send the service key over plain http.')
        self.key = key
        self.dry_run = dry_run
        self._opener = urllib.request.build_opener(self._NoRedirect())

    def _headers(self, extra=None):
        h = {
            'apikey': self.key,
            'Authorization': f'Bearer {self.key}',
            'Content-Type': 'application/json',
        }
        h.update(extra or {})
        return h

    def _post(self, path, payload, headers=None):
        # allow_nan=False deliberately: Python's json emits bare NaN/Infinity, which
        # is not valid JSON and which PostgREST rejects with an opaque error. Better
        # to fail here, naming the problem.
        body = json.dumps(payload, allow_nan=False).encode('utf-8')
        req = urllib.request.Request(self.url + path, data=body,
                                     headers=self._headers(headers), method='POST')
        try:
            with self._opener.open(req, timeout=60) as r:
                return r.status, r.read().decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:800]
            # Never echo the key back, whatever the server said.
            raise RuntimeError(f'{path} -> HTTP {e.code}: {detail}') from None

    def upsert(self, table, rows, on_conflict, chunk=500):
        """PostgREST upsert. Chunked because a whole season is ~12,000 rows and one
        giant request is a single point of failure."""
        if not rows:
            return 0
        done = 0
        for i in range(0, len(rows), chunk):
            batch = rows[i:i + chunk]
            # Serialise even on a dry run, so a dry run actually proves the payload is
            # postable rather than only proving the code reached the call.
            json.dumps(batch, allow_nan=False)
            if self.dry_run:
                done += len(batch)
                continue
            self._post(f'/rest/v1/{table}?on_conflict={on_conflict}', batch,
                       {'Prefer': 'resolution=merge-duplicates,return=minimal'})
            done += len(batch)
        return done

    def rpc(self, name, args):
        json.dumps(args, allow_nan=False)
        if self.dry_run:
            return None
        _, body = self._post(f'/rest/v1/rpc/{name}', args)
        return body


def _jwt_payload(token):
    """Best-effort decode of a Supabase JWT's payload, purely so a wrong key can be
    rejected with a clear message instead of failing as a silent no-op. Returns ''
    for anything that isn't a readable JWT — newer sb_secret_… keys aren't."""
    import base64
    try:
        part = token.split('.')[1]
        part += '=' * (-len(part) % 4)
        return base64.urlsafe_b64decode(part).decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001
        return ''


def sql_val(v):
    return 'null' if v in (None, '') else str(v)


# --------------------------------------------------------------------------- audit

def run_audit(ci_season, gws):
    """Share of rows carrying a NON-ZERO value for every stat the scoring config
    depends on, gameweek by gameweek.

    Non-zero, not non-blank, and that distinction is the entire point. An
    uncollected stat in this feed does not arrive blank — it arrives as a literal
    '0' or '0.0' for every player, which a non-blank check scores as 100% healthy.
    That is exactly how the 2025/26 dispossessions / saves-inside-box / offsides
    problem in calibration-results.md section 1 hid in plain sight."""
    print(f'\nStat availability audit — {ci_season}')
    print('Share of rows with a non-zero value. Compare each column across '
          'gameweeks, not against 100%:\na genuinely rare stat is legitimately low '
          'everywhere; a stat that is 0% early and healthy later is not collected yet.\n')
    header = 'GW   rows  ' + ''.join(f'{a[:11]:>12}' for a in AUDITED)
    print(header)
    print('-' * len(header))
    totals = {a: [0, 0] for a in AUDITED}
    suspicious = set()
    early_zero = {a: True for a in AUDITED}
    late_alive = {a: False for a in AUDITED}
    n_gw = len([g for g in gws])
    for idx, gw in enumerate(gws):
        pms, _ = fetch_gw(ci_season, gw)
        if not pms:
            continue
        line = f'{gw:<4} {len(pms):>5}  '
        for a in AUDITED:
            if a not in pms[0]:
                line += f'{"MISSING":>12}'
                suspicious.add(a)
                continue
            nz = sum(1 for r in pms if num(r.get(a)) != 0)
            totals[a][0] += nz
            totals[a][1] += len(pms)
            if idx < max(1, n_gw // 4):
                early_zero[a] = early_zero[a] and nz == 0
            elif nz > 0:
                late_alive[a] = True
            line += f'{nz / len(pms) * 100:>11.0f}%'
        print(line)
    print('-' * len(header))
    print('ALL        ' + ''.join(
        f'{(totals[a][0] / totals[a][1] * 100 if totals[a][1] else 0):>11.0f}%' for a in AUDITED))

    for a in AUDITED:
        if early_zero.get(a) and late_alive.get(a):
            suspicious.add(a)
        if totals[a][1] and totals[a][0] == 0:
            suspicious.add(a)
    print()
    if suspicious:
        print('!! SUSPECT — absent early and alive later, or absent entirely:')
        for a in sorted(suspicious):
            print(f'     {a}')
        print('   Exclude these from scoring_config_v5.json rather than letting them')
        print('   quietly reward second-half-of-the-season players only.\n')
    else:
        print('No stat is absent early and alive later. Feed looks safe to score on.\n')


# ---------------------------------------------------------------------------- main

def discover_gws(ci_season, requested):
    if requested:
        return requested
    found = []
    for gw in range(1, 39):
        # only_404 again: if a fetch blips, "the newest gameweek doesn't exist yet"
        # and "the newest gameweek failed to download" look identical, and --latest N
        # would quietly sync the N gameweeks *before* the newest one and report success.
        pms = fetch_csv_or_none(
            ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}',
                   'playermatchstats.csv'), timeout=15, only_404=True)
        if pms:
            found.append(gw)
    return found


def main():
    args = sys.argv[1:]
    requested, audit, season_override = None, False, None
    push, dry_run, latest = False, False, None
    out_path = os.path.join(ROOT, 'gw_sync.sql')
    i = 0
    while i < len(args):
        if args[i] == '--gw' and i + 1 < len(args):
            spec = args[i + 1]
            if '-' in spec:
                a, b = spec.split('-')
                requested = list(range(int(a), int(b) + 1))
            else:
                requested = [int(spec)]
            i += 2
        elif args[i] == '--out' and i + 1 < len(args):
            out_path = args[i + 1]
            i += 2
        elif args[i] == '--season' and i + 1 < len(args):
            season_override = args[i + 1]
            i += 2
        elif args[i] == '--audit':
            audit = True
            i += 1
        elif args[i] == '--push':
            push = True
            i += 1
        elif args[i] == '--dry-run':
            dry_run = True
            i += 1
        elif args[i] == '--latest' and i + 1 < len(args):
            latest = int(args[i + 1])
            i += 2
        else:
            i += 1

    db = None
    if push:
        url = os.environ.get('SUPABASE_URL', '').strip()
        key = os.environ.get('SUPABASE_SERVICE_KEY', '').strip()
        missing = [n for n, v in (('SUPABASE_URL', url), ('SUPABASE_SERVICE_KEY', key)) if not v]
        if missing:
            print(f'--push needs {" and ".join(missing)} in the environment. '
                  f'Nothing was written.', file=sys.stderr)
            return 2
        # A publishable/anon key here writes nothing useful, so refuse it up front.
        # Two key formats are in the wild: the legacy JWT, which carries the role in
        # its payload, and the newer sb_publishable_… / sb_secret_… strings, which
        # are not JWTs and can only be told apart by prefix.
        payload = _jwt_payload(key)
        if key.startswith('sb_publishable_') or '"role":"anon"' in payload.replace(' ', ''):
            print('SUPABASE_SERVICE_KEY looks like the publishable/anon key, not the '
                  'service_role key. That key is blocked by row-level security and '
                  'would write nothing. Aborting.', file=sys.stderr)
            return 2
        try:
            db = Supabase(url, key, dry_run=dry_run)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f'Push mode -> {url}' + ('  (dry run, no writes)' if dry_run else ''))

    ci_season, fpl_season = pick_season(season_override)
    print(f'Core-Insights season {ci_season} · FPL mirror season {fpl_season}')

    gws = discover_gws(ci_season, requested)
    if not gws:
        print('No gameweek has published player match stats yet — nothing to do.')
        return 0

    if latest and not requested and not audit:
        # Re-syncing the last few gameweeks every day is what makes this self-healing:
        # a failed run is picked up by the next one, and stat corrections the feed
        # makes after the fact land too. Re-syncing all 38 daily would work but gets
        # slow and pointless late in the season.
        skipped = gws[:-latest]
        gws = gws[-latest:]
        if skipped:
            print(f'--latest {latest}: syncing gameweeks {gws}, '
                  f'leaving {len(skipped)} earlier gameweek(s) as already stored. '
                  f'Run without --latest for a full re-sync.')

    if audit:
        run_audit(ci_season, gws)
        return 0

    players = load_players(ci_season)
    team_by_id, team_by_code = load_teams(ci_season)
    fixtures, fixture_index, _ = load_fpl_fixtures(fpl_season)
    if not fixture_index:
        print('  ! FPL mirror fixtures unavailable — fixture_id will be null on every '
              'row (points are unaffected; the RPC joins on gw + player_id).')
    cfg = load_config(os.path.join(ROOT, 'scoring_config_v5.json'))

    understat_path = os.path.join(ROOT, f'understat_goals_{ci_season}.json')
    understat = None
    if os.path.exists(understat_path):
        with open(understat_path) as f:
            understat = json.load(f)
        print(f'Using goal-location overlay {os.path.basename(understat_path)} '
              f'({len(understat)} players)')
    else:
        print('No Understat overlay found — every goal scores at the inside-the-box '
              'rate (see fetch_understat_shots.py).')

    lines = [
        '-- ============================================================',
        f'--  Gameweek sync — season {ci_season}, gameweeks {gws}',
        '--  Source: FPL-Core-Insights (match stats) + FPL mirror (fixtures)',
        '--  Generated by sync_gameweek.py. Paste into the Supabase SQL Editor',
        '--  and Run. Safe to re-run for a gameweek already synced.',
        '-- ============================================================',
        '',
    ]

    gw_set = set(gws)
    fx = [f for f in fixtures if f['gw'] in gw_set]

    # Rows are built once, in the shape the table expects, and then either rendered
    # as SQL or POSTed. One source of truth for what gets written, so the pasted and
    # the automated paths can never diverge.
    fixture_payload = [{
        'id': f['id'], 'gw': f['gw'], 'home_club': f['home'], 'away_club': f['away'],
        'kickoff_time': f['kickoff_time'],
        'home_score': int(f['home_score']) if f['home_score'] not in (None, '') else None,
        'away_score': int(f['away_score']) if f['away_score'] not in (None, '') else None,
        'finished': bool(f['finished']),
    } for f in fx]

    # Fixtures go in before any player row, because gw_player_stats.fixture_id is a
    # foreign key onto this table. Done here rather than at the end so a run that
    # finds fixtures but no published stats yet still records the fixtures.
    if db is not None and fixture_payload:
        n = db.upsert('fixtures', fixture_payload, 'id')
        print(f'  fixtures        {n} rows upserted')

    if fx:
        lines.append('insert into public.fixtures')
        lines.append('  (id, gw, home_club, away_club, kickoff_time, home_score, away_score, finished, updated_at)')
        lines.append('values')
        lines.append(',\n'.join(
            '  ({id}, {gw}, {h}, {a}, {kt}, {hs}, {as_}, {fin}, now())'.format(
                id=f['id'], gw=f['gw'],
                h=sql_str(f['home']), a=sql_str(f['away']),
                kt=sql_str(f['kickoff_time']) if f['kickoff_time'] else 'null',
                hs=sql_val(f['home_score']), as_=sql_val(f['away_score']),
                fin='true' if f['finished'] else 'false') for f in fx))
        lines.append('on conflict (id) do update set')
        lines.append('  gw = excluded.gw, home_club = excluded.home_club, away_club = excluded.away_club,')
        lines.append('  kickoff_time = excluded.kickoff_time, home_score = excluded.home_score,')
        lines.append('  away_score = excluded.away_score, finished = excluded.finished, updated_at = now();')
        lines.append('')

    import pandas as pd

    total_rows = total_unmatched = 0
    synced = []
    for gw in gws:
        pms, pgs = fetch_gw(ci_season, gw)
        if not pms:
            print(f'  gw{gw}: no player match stats published yet, skipped')
            continue
        ci_matches = fetch_csv_or_none(
            ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}', 'matches.csv'))
        rows, unmatched = build_rows(gw, pms, pgs, players, team_by_id, team_by_code,
                                     fixture_index, ci_matches, understat)
        total_unmatched += unmatched
        if not rows:
            print(f'  gw{gw}: no matchable rows, skipped')
            continue

        df = pd.DataFrame(rows)
        scored = score_frame(df, cfg, keep_components=True)
        scored['starts'] = df['starts'].values
        scored['match_id'] = df['match_id'].values

        lines.append(f'-- gameweek {gw}: {len(scored)} players')
        lines.append('insert into public.gw_player_stats')
        lines.append('  (gw, player_id, fixture_id, pos, minutes, starts, points, breakdown, updated_at)')
        lines.append('values')
        comp = [c for c in scored.columns if c.startswith('pts_')]
        vals = []
        gw_payload = []
        for _, r in scored.iterrows():
            breakdown = {c[4:]: float(r[c]) for c in comp if r[c]}
            fixture_id = (int(r['match_id'])
                          if r['match_id'] not in (None, '') and not pd.isna(r['match_id'])
                          else None)
            gw_payload.append({
                'gw': gw, 'player_id': int(r['player_id']), 'fixture_id': fixture_id,
                'pos': r['pos'], 'minutes': int(r['minutes']),
                'starts': bool(r['starts']), 'points': float(r['score']),
                'breakdown': breakdown,
            })
            vals.append('  ({gw}, {pid}, {fx}, {pos}, {mins}, {starts}, {pts}, {bd}::jsonb, now())'.format(
                gw=gw, pid=int(r['player_id']),
                fx=sql_val(r['match_id']) if r['match_id'] not in (None, '') and
                   not pd.isna(r['match_id']) else 'null',
                pos=sql_str(r['pos']), mins=int(r['minutes']),
                starts='true' if r['starts'] else 'false',
                pts=float(r['score']),
                bd=sql_str(json.dumps(breakdown))))
        lines.append(',\n'.join(vals))
        lines.append('on conflict (gw, player_id) do update set')
        lines.append('  fixture_id = excluded.fixture_id, pos = excluded.pos, minutes = excluded.minutes,')
        lines.append('  starts = excluded.starts, points = excluded.points, breakdown = excluded.breakdown,')
        lines.append('  updated_at = now();')
        lines.append('')
        total_rows += len(scored)
        synced.append(gw)

        # Each gameweek is written and recomputed as a unit. Doing every upsert first
        # and every recompute afterwards means a failure part-way leaves earlier
        # gameweeks with fresh stats and stale gw_points — and since the daily job
        # only revisits the last few gameweeks, an early one could stay wrong forever
        # with nothing to signal it. Per-gameweek, a failure stops the run with every
        # completed gameweek fully consistent.
        if db is not None:
            db.upsert('gw_player_stats', gw_payload, 'gw,player_id')
            db.rpc('compute_all_gw_points', {'p_gw': gw})
            print(f'  gw{gw}: {len(scored)} players scored, written and recomputed'
                  + (f', {unmatched} unmatched' if unmatched else ''))
        else:
            print(f'  gw{gw}: {len(scored)} players scored'
                  + (f', {unmatched} unmatched' if unmatched else ''))

    # Only gameweeks that actually produced rows. Recomputing a gameweek with no
    # stats writes every manager a legitimate-looking zero — which is exactly what
    # `--gw 1-38` used to do for every future gameweek of the season.
    lines.append("-- Recompute every manager's points for the gameweeks just synced.")
    for gw in synced:
        lines.append(f'select public.compute_all_gw_points({gw});')
    lines.append('')

    if not total_rows:
        print('\nNo player rows produced — no points written.'
              + (' Fixtures were still updated.' if (db is not None and fixture_payload)
                 else ''))
        return 0

    if db is None:
        with open(out_path, 'w') as f:
            f.write('\n'.join(lines))
        print(f'\nWrote {out_path} — {total_rows} player-gameweek rows across '
              f'{len(synced)} gameweek(s)'
              + (f', {total_unmatched} unmatched' if total_unmatched else '') + '.')
        print('Paste it into the Supabase SQL Editor and Run.')
        return 0

    print(f'\nDone — {total_rows} player-gameweek rows across {len(synced)} gameweek(s)'
          + (f', {total_unmatched} unmatched' if total_unmatched else '')
          + ('  [dry run — nothing actually written]' if dry_run else '') + '.')
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
