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
        - data/{season}/By Tournament/Premier League/GW{n}/shots.csv
              one row per shot, keyed on the same FPL player_id as the match feed,
              carrying start_x / start_y pitch coordinates, `outcome` and `situation`.
              This is what splits goals into inside-the-box (80) and outside-the-box
              (100) and what supplies hit_woodwork (outcome == 'post'). It sits in the
              same gameweek folder as playermatchstats.csv, so it costs one extra
              fetch per gameweek and adds no new source, no scraper and no schedule.
        - data/{season}/players.csv   player_id -> player_code (the permanent FPL
              code this game uses as its player_id) and position.

    Fixtures table: vaastav/Fantasy-Premier-League's fixtures.csv, so `fixtures.id`
        stays the FPL fixture id the schema comment promises. Core-Insights matches
        are mapped onto those ids by (gameweek, home team, away team) — both mirrors
        use identical FPL team ids.

WHAT THIS FIXES
    The previous version read only the vaastav mirror, which carries the official FPL
    fields and nothing else. Measured across all 11,492 player-appearances of the
    2025/26 season, that delivered only 73.1% of the points scoring_config_v5.json
    defines — and unevenly: GK 86.9%, DEF 80.9%, MID 66.0%, FWD 60.8%. Since starting
    prices were modelled on the full stat set, every breakeven was unreachable and the
    shortfall was position-biased, which would have repriced the whole market toward
    defenders. This version feeds every line in the config real data.

WHAT SHOTS.CSV CLOSED (2026-08-21)
    Every non-zero line in scoring_config_v5.json now has a source. The last two gaps
    were goal_outside_box (100 vs 80, 2.2% of points) and hit_woodwork (5, 0.2%), and
    both were closed by a file that had been sitting unused in the same Core-Insights
    gameweek folders all along. An earlier attempt to close the first one by scraping
    Understat is gone: it needed a second data source, a second scheduled job, a
    page-format-dependent regex and fuzzy player-name matching, and none of those are
    necessary when the shot rows are already keyed on FPL player ids.

    One honest caveat: hit_woodwork is credited at about 73% of the truth. 211 shots
    carry outcome == 'post' in 2025/26 against 288 the same feed reports at team
    level, and nothing per-shot separates the other 77 from an ordinary near-miss.
    --audit prints both numbers every run. See reconcile_woodwork().

    Still genuinely unavailable free, and still zeroed in the config: big chances
    created, errors leading to a chance or goal, penalties won, successful corners,
    passes into the box, passes received in the box.

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
import math
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


# Feed columns that score points AND are common enough that a gameweek with none
# of them at all is a broken feed rather than an unusual round. The percentages
# are each column's non-zero share across the whole of 2025/26, measured with
# --audit; every one is comfortably into double figures, so 0% in a gameweek of
# 400 rows cannot happen by chance.
#
# WHY THIS RUNS ON EVERY SYNC RATHER THAN ONLY UNDER --audit:
# The scheduled job has never run --audit, and in 2026/27 GW1 the provider
# shipped `tackles` and `successful_dribbles` as present-but-entirely-empty
# columns. Nothing errored. defcon_cbit is tackles+interceptions+blocks+
# clearances at 3 points each and defcon is about a fifth of all scoring, so
# roughly 6% of the league's points quietly stopped existing — concentrated on
# defenders, which is exactly the position-biased distortion part 4 was written
# to prevent, and it would have gone into the price economy at GW3.
#
# This is the same lesson as the shots fail-safe: a silent zero is the dangerous
# failure, so make it loud on every single run.
COMMON_SCORING_STATS = {
    'chances_created':        ('chance_created', 30),
    'touches_opposition_box': ('touch_opp_box', 44),
    'successful_dribbles':    ('successful_dribble', 25),
    'accurate_crosses':       ('accurate_cross', 15),
    'aerial_duels_won':       ('aerial_won', 37),
    'was_fouled':             ('was_fouled', 35),
    'fouls_committed':        ('foul_committed', 39),
    'dribbled_past':          ('dribbled_past', 24),
    'shots_on_target':        ('shot_on_target', 18),
    # A tuple key means "any one of these columns carrying data is fine". The
    # provider dropped the attempted/won tackle split for 2026/27: `tackles` and
    # `tackles_won_percent` are blank on every row and the single remaining
    # figure is published as `tackles_won`. derive() reads whichever is present,
    # so the alarm should only sound when BOTH are empty. See defcon_cbit in
    # scoring.py.
    ('tackles', 'tackles_won'): ('defcon_cbit', 35),
    'interceptions':          ('defcon_cbit', 26),
    'blocks':                 ('defcon_cbit', 14),
    'clearances':             ('defcon_cbit', 43),
    'recoveries':             ('recovery', 66),
    'saves':                  ('save', 5),
}
COVERAGE_MIN_ROWS = 100          # below this a gameweek is too partial to judge


def check_stat_coverage(gw, pms, cfg=None):
    """Warn when a stat that earns points is completely absent from a gameweek.

    Returns the list of dead column names, so callers can decide what to do.
    Deliberately does NOT block the write: the points are still mostly right, and
    refusing to publish a gameweek over this would leave managers with nothing at
    all. Loud, not fatal.
    """
    rows = pms or []
    if len(rows) < COVERAGE_MIN_ROWS:
        return []
    scored = set()
    if cfg:
        scored = {k for k, v in cfg.get('stats', {}).items()
                  if (max(v.values()) if isinstance(v, dict) else v)}
    dead = []
    for col, (stat, expected) in COMMON_SCORING_STATS.items():
        if scored and stat not in scored:
            continue                       # not scored in this config, so not a problem
        # A tuple key lists interchangeable spellings of the same underlying
        # stat; the feed only has to publish one of them.
        cols = col if isinstance(col, tuple) else (col,)
        n = 0
        for r in rows:
            for c in cols:
                v = (r.get(c) or '').strip() if isinstance(r.get(c), str) else r.get(c)
                try:
                    if v not in (None, '') and float(v) != 0:
                        n += 1
                        break
                except (TypeError, ValueError):
                    continue
            if n:
                break
        if n == 0:
            dead.append((' / '.join(cols), stat, expected))
    if dead:
        for col, stat, expected in dead:
            print(f'  !! gw{gw}: `{col}` is empty for all {len(rows)} rows. It was '
                  f'non-zero on ~{expected}% of appearances in 2025/26 and it feeds '
                  f'`{stat}`, which scores. Those points are NOT being awarded. '
                  f'Check whether the provider has renamed or dropped the column.',
                  file=sys.stderr)
    return [c for c, _s, _e in dead]


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


def finished_without_stats(ci_matches, pms_match_ids, team_by_code):
    """Matches the feed calls finished but has published no player rows for.

    Returned as a sorted list of 'HUL v AVL' labels, empty when the gameweek is
    whole. Split out of the sync loop so it can be tested directly; see the long
    note at the call site for why it matters.

    `finished` is read leniently ('True', 'true', '1', True) because the two
    feeds have never agreed on how to spell a boolean, and team ids arrive as
    floats ('88.0') here while teams.csv writes them as integers — pid_key
    reconciles both. A match whose clubs cannot be resolved is still reported,
    with '?' for the unknown side: a gap we cannot name is still a gap.
    """
    out = []
    for m in ci_matches or ():
        if str(m.get('finished', '')).strip().lower() not in ('true', '1'):
            continue
        if m.get('match_id') in pms_match_ids:
            continue
        h = team_by_code.get(pid_key(m.get('home_team'))) or '?'
        a = team_by_code.get(pid_key(m.get('away_team'))) or '?'
        out.append(f'{h} v {a}')
    return sorted(out)


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

def pid_key(v):
    """Normalise a Core-Insights player_id to one canonical string form.

    The provider is not consistent about how it spells this id, and the
    inconsistency is silent rather than fatal. In 2025/26 every feed wrote plain
    integers ('14'). In 2026/27 shots.csv writes floats ('14.0') while
    playermatchstats.csv still writes integers, so a bare string compare against
    players.csv resolved 0 of 277 shot rows in GW1 — every goal fell into the
    inside-the-box bucket at 80 and woodwork never scored at all.

    Normalising BOTH the dict keys and every lookup through here means the match
    survives either spelling, in either feed, in either direction. Anything that
    isn't a whole number is returned untouched rather than guessed at.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s)
    except (TypeError, ValueError):
        return s
    if not math.isfinite(f) or f != int(f):
        return s
    return str(int(f))


def load_players(ci_season):
    """player_id (Core-Insights, season-scoped) -> {code, position, web_name}."""
    rows = fetch_csv(ci_url(ci_season, 'players.csv'))
    out = {}
    for r in rows:
        key = pid_key(r.get('player_id'))
        if key is None:
            continue
        try:
            out[key] = {
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
            fid = int(float(r['id']))
            gw = int(float(r['event'])) if r.get('event') else None
            home = id_to_short.get(str(int(float(r['team_h']))))
            away = id_to_short.get(str(int(float(r['team_a']))))
        except (KeyError, ValueError, TypeError):
            continue
        f = {
            'id': fid, 'gw': gw, 'home': home, 'away': away,
            'kickoff_time': r.get('kickoff_time') or None,
            # Coerced here, not at the point of use: everything downstream then
            # gets a clean int-or-None and cannot be surprised by a float
            # spelling. See int_or_none().
            'home_score': int_or_none(r.get('team_h_score')),
            'away_score': int_or_none(r.get('team_a_score')),
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


def int_or_none(v):
    """Coerce a feed value to int, tolerating float spellings ('3.0') and blanks.

    Feed providers reformat columns without warning and it has bitten this
    project twice now. Core-Insights started writing shots.csv player ids as
    '14.0' where players.csv still said '14', which silently discarded 100% of
    shots; then the vaastav mirror started writing fixture scores as '3.0'
    where it used to write '3', and a bare int() raised ValueError right in the
    middle of building the fixtures payload — killing the whole push before a
    single point was written.

    Anything unparseable becomes None rather than a wrong number, because a
    missing score renders as "not known yet" while a wrong one is a lie.
    """
    if v in (None, '', 'None'):
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


MATCH_SUM_KEYS = [
    'minutes_played', 'goals', 'assists', 'penalties_scored', 'penalties_missed',
    'shots_on_target', 'chances_created', 'touches_opposition_box',
    'successful_dribbles', 'accurate_crosses', 'was_fouled', 'fouls_committed',
    'dribbled_past', 'aerial_duels_won', 'ground_duels_won', 'tackles',
    # `tackles_won` is carried alongside `tackles` because the provider stopped
    # publishing the latter in 2026/27; derive() falls back to it. Summing both
    # is harmless when both are present — only one is ever read.
    'tackles_won',
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
    """Return (playermatchstats rows, player_gameweek_stats rows, shots rows, status).

    `status` is 'ok', 'absent' (a real 404 — the feed hasn't published shots for this
    gameweek) or 'error' (a timeout, a 503, anything else). The distinction matters:
    a 404 during a gameweek still being played is normal, while an error means the
    file may well exist and be perfectly good. Collapsing the two is how a transient
    blip would silently rewrite a correctly-scored gameweek with every goal at 80.
    """
    pms = fetch_csv_or_none(
        ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}', 'playermatchstats.csv'))
    pgs = fetch_csv_or_none(
        ci_url(ci_season, 'By Gameweek', f'GW{gw}', 'player_gameweek_stats.csv'))
    try:
        shots = fetch_csv_or_none(
            ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}', 'shots.csv'),
            only_404=True)
        status = 'absent' if shots is None else 'ok'
    except Exception as e:  # noqa: BLE001
        print(f'  ! gw{gw}: shots.csv could not be fetched ({e})', file=sys.stderr)
        shots, status = None, 'error'
    return pms, pgs, shots, status


# ------------------------------------------------------------------- shot location

# Where the edge of the penalty area sits in Core-Insights' shot coordinates.
#
# start_y is unambiguously a 0-100 scale across the pitch width: all 92 penalties in
# 2025/26 sit at exactly y = 50.0. start_x runs from the goal line being attacked and
# is *close* to metres — penalties sit at x = 11.5 against a real penalty spot of
# 10.97m — but not exactly, so the boundary is calibrated rather than assumed.
#
# The calibration is against the provider's own numbers. matches.csv publishes
# home/away_shots_inside_box and _outside_box per match; classifying all 9,504 shots
# of 2025/26 with the values below reproduces those counts exactly for 751 of 760
# team-matches (98.8%), season totals 6,409 inside vs 6,402 reported. Sweeping the
# threshold shows a clear plateau at 16.9-17.0 — 16.5 scores 92.5% and treating
# start_x as a percentage of a 105m pitch (15.71) scores 73.4%, so this is well
# identified, not a fudge. The y bounds are the true geometric ones (a 40.32m box on
# a 68m pitch); agreement is flat across 20.3-22.0 because almost nothing is shot
# from that sliver, so the honest geometric value is used.
#
# reconcile_shot_box() re-runs this check on EVERY sync, not only under --audit, and
# a gameweek that fails it has its split discarded (every goal reverts to 80) with a
# loud warning.
#
# HOW THE CHECK IS MEASURED — changed 2026-09-06, and the old way was wrong.
#
# It used to require whole TEAM-MATCHES to match exactly, and pass only if 70% of
# them did. A team-match is about 7 shots, so a single shot sitting within a metre
# of the line flips the whole team-match from "exact" to "wrong". That is a
# hair-trigger, and it fired: 2026/27 GW2 scored 13/20 = 65%, its goal split was
# thrown away, and every goal in the round paid 80 with no woodwork — while the
# actual classification was 184 inside against the provider's 186, i.e. **99.3%
# right**. Four outside-box goals and two woodwork hits were silently dropped for
# no reason.
#
# It is now measured per SHOT: the total misclassified shots as a share of all
# shots. That is the quantity that actually matters, because a goal is credited
# per shot, and it is immune to the team-match granularity problem. A genuine
# rescale — the thing this defends against — still scores near 1.0: shifting every
# start_x by +40 misclassifies essentially every shot.
#
# Measured error rates: 2026/27 GW1 0.4%, GW2 3.3%, GW3 2.1%; worst 2025/26
# gameweek well inside the limit. 10% leaves a wide margin over real noise while
# catching anything structural.
SHOT_BOX_MAX_SHOT_ERROR = 0.10
SHOT_BOX_X = 16.95
SHOT_BOX_Y_LO = (68 - 40.32) / 2 / 68 * 100      # 20.35
SHOT_BOX_Y_HI = 100 - SHOT_BOX_Y_LO              # 79.65


def shot_inside_box(row):
    """True if the shot was taken inside the penalty area. Missing, unparseable, NaN
    or infinite coordinates return True — the inside rate (80) is both the common
    case and the lower of the two, so an unknown never inflates a score.

    NaN is checked explicitly and not left to the comparison: float('nan') parses
    without raising, and every comparison against it is False, so `x <= SHOT_BOX_X`
    would quietly classify it as OUTSIDE and pay 100. 'NaN' is exactly what a pandas
    CSV writer emits for a null with a non-default na_rep, so this is a live risk,
    not a theoretical one."""
    try:
        x = float(row['start_x'])
        y = float(row['start_y'])
    except (KeyError, TypeError, ValueError):
        return True
    if not (math.isfinite(x) and math.isfinite(y)):
        return True
    return x <= SHOT_BOX_X and SHOT_BOX_Y_LO <= y <= SHOT_BOX_Y_HI


def summarise_shots(shots, players, match_ids=None):
    """shots.csv rows -> ({player_code: {'outside': n, 'post': n}}, unresolved, unattributed).

    `unresolved` counts shots carrying a player_id that players.csv does not know —
    the signal that the id space has moved. `unattributed` counts shots the feed
    published with no player_id at all, which is normal and is reported separately
    so it can never be mistaken for the former.

    Only the outside-the-box count is carried, not the inside one: playermatchstats
    is authoritative for how many goals a player scored, so inside is derived as
    (open-play goals - outside) in build_rows(). That way a shot row the feed is
    missing costs 20 points, never a phantom goal.

    `match_ids` restricts the shots considered to matches that playermatchstats has
    also published. Without it the two feeds can disagree about WHICH match a goal
    came from: if shots.csv carries an outside-the-box goal from a match whose player
    stats aren't published yet, while pms carries an inside-the-box goal from a
    different match in the same gameweek, the count-only clamp in build_rows() pays
    the inside goal at 100. matches.csv publishes `stats_processed` and
    `player_stats_processed` as separate flags — 20 matches in 2025/26 have them
    disagreeing — so the two feeds demonstrably do move independently.

    Penalties are excluded — scoring_config_v5.json prices a penalty at the
    inside-the-box rate and build_rows() counts them separately. Own goals are
    neither 'goal' nor 'post' here, so they neither create a goal nor credit
    woodwork; they are credited to the attacking player by the feed, and the
    scoring config handles own goals from the FPL feed instead.
    """
    extra = {}
    unresolved = 0
    unattributed = 0
    for r in shots or []:
        if match_ids is not None and r.get('match_id') not in match_ids:
            continue
        key = pid_key(r.get('player_id'))
        if key is None:
            # The feed itself leaves player_id blank on some shots (35 of 277 in
            # 2026/27 GW1). Nothing can be credited for those, but they are NOT
            # evidence the id space has changed — counting them as unresolved is
            # what would turn a healthy run into a permanent false alarm.
            unattributed += 1
            continue
        meta = players.get(key)
        if meta is None:
            unresolved += 1
            continue
        code = meta['code']
        e = extra.setdefault(code, {'outside': 0, 'post': 0})
        outcome = (r.get('outcome') or '').strip()
        if outcome == 'post':
            e['post'] += 1
        elif outcome == 'goal':
            if (r.get('situation') or '').strip() == 'penalty':
                continue
            if not shot_inside_box(r):
                e['outside'] += 1
    return extra, unresolved, unattributed


def reconcile_shot_box(shots, matches, match_ids=None):
    """Check shot_inside_box() against the provider's own published team totals.

    matches.csv carries home/away_shots_inside_box and _outside_box per match, which
    is independent ground truth for the classification. This is the only defence
    against the provider silently rescaling start_x / start_y — a change there would
    not error, it would just start splitting goals wrongly and quietly move 20 points
    a goal around the price economy.

    Own goals are deliberately INCLUDED. The feed attributes them to the attacking
    player, counts them in that team's shots_on_target, and counts them in the team
    box totals; excluding them here manufactures mismatches (2 of them in 2025/26).

    Returns (exact, total, got_in, got_out, rep_in, rep_out, off_by).
    """
    counted = {}
    for r in shots or []:
        if match_ids is not None and r.get('match_id') not in match_ids:
            continue
        key = (r.get('match_id'), (r.get('is_home') or '').strip())
        c = counted.setdefault(key, [0, 0])
        c[0 if shot_inside_box(r) else 1] += 1

    exact = total = got_in = got_out = rep_in = rep_out = off_by = 0
    for m in matches or []:
        if match_ids is not None and m.get('match_id') not in match_ids:
            continue
        for side, flag in (('home', 'True'), ('away', 'False')):
            try:
                ri = int(float(m[f'{side}_shots_inside_box']))
                ro = int(float(m[f'{side}_shots_outside_box']))
            except (KeyError, TypeError, ValueError):
                continue
            gi, go = counted.get((m.get('match_id'), flag), [0, 0])
            total += 1
            got_in += gi
            got_out += go
            rep_in += ri
            rep_out += ro
            if gi == ri and go == ro:
                exact += 1
            else:
                off_by += abs(gi - ri) + abs(go - ro)
    return exact, total, got_in, got_out, rep_in, rep_out, off_by


def reconcile_woodwork(shots, matches, match_ids=None):
    """Same idea for hit_woodwork: outcome == 'post' against home/away_hit_woodwork.

    Returns (ours, reported). These do NOT agree. Across 2025/26 the feed reports 288
    woodwork hits at team level while only 211 shots carry outcome == 'post' — the
    other 77 are coded 'miss', and nothing in goal_mouth_location or goal_mouth_z
    separates them from an ordinary near-miss (checked: no post-specific value
    exists). So woodwork is credited at about 73% of the truth. That is deliberate
    and documented rather than silently wrong: hit_woodwork is 0.2% of all points,
    the shortfall is roughly position-neutral, and under-crediting 27% of it moves a
    player by about 1 point a season. Scoring 73% of it is closer to the prices the
    game was modelled on than scoring none of it, which is what happened before."""
    ours = reported = 0
    for r in shots or []:
        if match_ids is not None and r.get('match_id') not in match_ids:
            continue
        if (r.get('outcome') or '').strip() == 'post':
            ours += 1
    for m in matches or []:
        if match_ids is not None and m.get('match_id') not in match_ids:
            continue
        for side in ('home', 'away'):
            try:
                reported += int(float(m[f'{side}_hit_woodwork']))
            except (KeyError, TypeError, ValueError):
                continue
    return ours, reported


def build_rows(gw, pms, pgs, players, team_by_id, team_by_code, fixture_index,
               ci_matches, shot_extra):
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
        meta = players.get(pid_key(r.get('player_id')))
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
        meta = players.get(pid_key(r.get('id')))
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

        # Goal location and woodwork, from shots.csv. playermatchstats stays
        # authoritative for the goal count; shots.csv only says how many of those
        # goals were struck from outside the area. Clamping to open_play means a
        # disagreement between the two feeds can never invent a goal — the worst it
        # can do is score one at 80 instead of 100.
        extra = (shot_extra or {}).get(code) or {}
        pens = row['penalties_scored']
        open_play = max(0, row['goals'] - pens)
        outside = min(open_play, max(0, num(extra.get('outside'))))
        row['goal_inside_box'] = open_play - outside
        row['goal_outside_box'] = outside
        row['goal_penalty'] = pens
        row['hit_woodwork'] = max(0, num(extra.get('post')))
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

    def _get(self, path, headers=None):
        req = urllib.request.Request(self.url + path,
                                     headers=self._headers(headers), method='GET')
        try:
            with self._opener.open(req, timeout=60) as r:
                return r.status, r.read().decode('utf-8', 'replace'), dict(r.headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:800]
            raise RuntimeError(f'{path} -> HTTP {e.code}: {detail}') from None

    def select(self, table, columns='*', query='', page=1000):
        """Read every row of a table, following PostgREST's pagination.

        PostgREST caps a response (Supabase's default max-rows is 1000) and says
        so ONLY in the Content-Range header — an over-long result comes back as a
        200 with a short body, not an error. Reading gw_player_stats for a whole
        season is ~21,000 rows, so a single unpaged GET would silently return the
        first 1,000 and the pricing job would reprice the league off a fifth of
        the evidence, with nothing anywhere to indicate it. Hence explicit paging
        with an exact-count assertion at the end.
        """
        rows = []
        offset = 0
        while True:
            q = f'/rest/v1/{table}?select={urllib.parse.quote(columns)}'
            if query:
                q += '&' + query
            status, body, headers = self._get(
                q, {'Range-Unit': 'items',
                    'Range': f'{offset}-{offset + page - 1}',
                    'Prefer': 'count=exact'})
            batch = json.loads(body) if body.strip() else []
            rows.extend(batch)
            cr = headers.get('Content-Range', '')
            total = None
            if '/' in cr:
                tail = cr.rsplit('/', 1)[1].strip()
                if tail.isdigit():
                    total = int(tail)
            if not batch:
                break
            offset += len(batch)
            # Advance by what the server ACTUALLY returned, and trust the count
            # over the requested page size. PostgREST's own db-max-rows can be
            # lower than the range we asked for, so "we got back fewer rows than
            # we asked for" does NOT mean "that was the last page" — treating it
            # that way silently truncates the read to one server page and would
            # reprice the league off a fraction of the season.
            if total is not None:
                if offset >= total:
                    break
            elif len(batch) < page:
                break
            if offset > 500000:
                raise RuntimeError(f'{table}: pagination did not terminate')
        if total is not None and len(rows) != total:
            raise RuntimeError(
                f'{table}: read {len(rows)} rows but the server reports {total}. '
                f'Refusing to continue — a partial read here would silently reprice '
                f'the league off incomplete data.')
        return rows

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
    shots_by_gw = {}
    for idx, gw in enumerate(gws):
        pms, _, shots, _status = fetch_gw(ci_season, gw)
        shots_by_gw[gw] = shots
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

    audit_shot_coordinates(ci_season, gws, shots_by_gw)


def audit_shot_coordinates(ci_season, gws, shots_by_gw):
    """Season-wide version of the per-gameweek check main() already runs: does
    shot_inside_box() reproduce the inside/outside-box counts the feed publishes at
    team level, and does outcome == 'post' reproduce its woodwork counts?

    On 2025/26 the box split agrees exactly for 751 of 760 team-matches. Anything
    below about 95% means the coordinates have moved and SHOT_BOX_X needs
    recalibrating — main() independently refuses to apply a split below
    SHOT_BOX_MAX_SHOT_ERROR, so a rescale costs 20 points a goal, never 100 points on
    a tap-in."""
    print('Shot reconciliation against the feed\'s own team-level totals')
    exact = total = got_in = got_out = rep_in = rep_out = off_by = 0
    wood_ours = wood_rep = 0
    gws_with_shots = 0
    for gw in gws:
        shots = shots_by_gw.get(gw)
        matches = fetch_csv_or_none(
            ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}', 'matches.csv'))
        if not shots or not matches:
            continue
        gws_with_shots += 1
        e, t, gi, go, ri, ro, off = reconcile_shot_box(shots, matches)
        exact += e
        total += t
        got_in += gi
        got_out += go
        rep_in += ri
        rep_out += ro
        off_by += off
        wo, wr = reconcile_woodwork(shots, matches)
        wood_ours += wo
        wood_rep += wr
    if not total:
        print('  no gameweek in this range has both shots.csv and matches.csv — '
              'nothing to reconcile. Goals all score at the inside-box rate.\n')
        return
    pct = exact / total * 100
    print(f'  {gws_with_shots} gameweek(s) · {total} team-matches · '
          f'{exact} classified exactly as the feed reports ({pct:.1f}%), '
          f'{off_by} shot(s) misplaced in the rest')
    print(f'  box split: inside {got_in} vs {rep_in} reported · '
          f'outside {got_out} vs {rep_out} reported')
    if pct < 95:
        print(f'  !! BELOW 95%. The provider has probably rescaled start_x/start_y. '
              f'Recalibrate SHOT_BOX_X. Until then main() will refuse to apply the '
              f'split and every goal scores 80.')
    else:
        print('  Coordinates behave as calibrated. The 80/100 goal split is sound.')
    share = (wood_ours / wood_rep * 100) if wood_rep else 0
    print(f'  woodwork:  {wood_ours} shots with outcome == \'post\' vs {wood_rep} '
          f'reported at team level ({share:.0f}% credited)')
    print('  The woodwork shortfall is known and accepted — the feed codes the rest as '
          'ordinary misses with nothing to tell them apart. hit_woodwork is 0.2% of\n'
          '  all points, so 27% of it is roughly 1 point per player per season.\n')



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

    # ---- results come from Core-Insights, not from the FPL mirror ----------------
    # The vaastav mirror is not being maintained for 2026/27: six days after GW1 was
    # played, every one of its ten fixtures still reads finished=False with both
    # scores blank. Taken at face value that means fixtures.finished is false for
    # matches that finished last week, which breaks three things at once —
    # gw_scoring_progress() reports "0 of 10 finished", the client's gwAllScored()
    # fallback can never fire, and the pricing job's "is this gameweek complete?"
    # test never becomes true, so prices would never move at all.
    #
    # Core-Insights' own matches.csv carries finished, home_score and away_score and
    # is current — it is the same file already used for the shot reconciliation, and
    # it is joined the same way (team CODES via short names; see load_fpl_fixtures).
    # The mirror still supplies fixture IDs and kick-off times, because fixtures.id
    # must remain an FPL fixture id.
    results = {}
    for gw in gws:
        for m in (fetch_csv_or_none(
                ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}',
                       'matches.csv')) or []):
            try:
                h = team_by_code.get(str(int(float(m['home_team']))))
                a = team_by_code.get(str(int(float(m['away_team']))))
                g = int(float(m['gameweek']))
            except (KeyError, ValueError, TypeError):
                continue
            if not (h and a):
                continue
            fid = fixture_index.get((g, h, a)) or fixture_index.get((g, a, h))
            if fid is None:
                continue

            _score = int_or_none
            results[fid] = {
                'finished': (str(m.get('finished') or '').strip().lower() == 'true'),
                'home_score': _score(m.get('home_score')),
                'away_score': _score(m.get('away_score')),
            }

    def _pick(f, key, mirror_val):
        """Prefer the live feed; fall back to the mirror rather than to nothing."""
        r = results.get(f['id'])
        if r is None:
            return mirror_val
        v = r[key]
        if key == 'finished':
            # Never un-finish a match. If either source says it is done, it is done.
            return bool(v or mirror_val)
        return v if v is not None else mirror_val

    # Rows are built once, in the shape the table expects, and then either rendered
    # as SQL or POSTed. One source of truth for what gets written, so the pasted and
    # the automated paths can never diverge.
    fixture_payload = [{
        'id': f['id'], 'gw': f['gw'], 'home_club': f['home'], 'away_club': f['away'],
        'kickoff_time': f['kickoff_time'],
        'home_score': _pick(f, 'home_score', f['home_score']),
        'away_score': _pick(f, 'away_score', f['away_score']),
        'finished': _pick(f, 'finished', bool(f['finished'])),
    } for f in fx]

    n_fin = sum(1 for r in fixture_payload if r['finished'])
    if results:
        print(f'  results overlay: {len(results)} match(es) from Core-Insights, '
              f'{n_fin} of {len(fixture_payload)} fixture(s) marked finished')
    elif fx:
        print('  ! no Core-Insights results available; falling back to the FPL '
              'mirror, which has not been updated for 2026/27.', file=sys.stderr)

    # Fixtures go in before any player row, because gw_player_stats.fixture_id is a
    # foreign key onto this table. Done here rather than at the end so a run that
    # finds fixtures but no published stats yet still records the fixtures.
    if db is not None and fixture_payload:
        n = db.upsert('fixtures', fixture_payload, 'id')
        print(f'  fixtures        {n} rows upserted')

    # Rendered from fixture_payload, NOT from fx. The comment above claims one source
    # of truth so the pasted and automated paths cannot diverge — but this block used
    # to read the raw mirror rows, so it was quietly the one place the claim was
    # false. The results overlay made that visible: the push wrote finished=true and
    # the pasted SQL wrote finished=false for the same ten matches.
    if fixture_payload:
        lines.append('insert into public.fixtures')
        lines.append('  (id, gw, home_club, away_club, kickoff_time, home_score, away_score, finished, updated_at)')
        lines.append('values')
        lines.append(',\n'.join(
            '  ({id}, {gw}, {h}, {a}, {kt}, {hs}, {as_}, {fin}, now())'.format(
                id=f['id'], gw=f['gw'],
                h=sql_str(f['home_club']), a=sql_str(f['away_club']),
                kt=sql_str(f['kickoff_time']) if f['kickoff_time'] else 'null',
                hs=sql_val(f['home_score']), as_=sql_val(f['away_score']),
                fin='true' if f['finished'] else 'false') for f in fixture_payload))
        lines.append('on conflict (id) do update set')
        lines.append('  gw = excluded.gw, home_club = excluded.home_club, away_club = excluded.away_club,')
        lines.append('  kickoff_time = excluded.kickoff_time, home_score = excluded.home_score,')
        lines.append('  away_score = excluded.away_score, finished = excluded.finished, updated_at = now();')
        lines.append('')

    import pandas as pd

    # One pass to see whether this season has a shot feed at all. 2025/26 publishes
    # shots.csv for all 38 gameweeks; a brand-new season publishes none of it until
    # the first match finishes. The two cases need opposite handling and cannot be
    # told apart one gameweek at a time:
    #   - season has no shot feed  -> score every goal at 80 and keep going. Refusing
    #     to write points at all would be far worse than a 20-point shortfall.
    #   - season has a shot feed but this gameweek is missing it -> anomalous. Do NOT
    #     write, because writing would overwrite correct 100-point goals with 80s and
    #     zero out woodwork on a gameweek that was already scored properly.
    gw_data = {}
    season_has_shots = False
    for gw in gws:
        gw_data[gw] = fetch_gw(ci_season, gw)
        if gw_data[gw][3] == 'ok':
            season_has_shots = True
    if not season_has_shots:
        print('  ! No gameweek in this run has shots.csv. Every goal will score at the '
              'inside-the-box rate (80) and no woodwork will be credited — the '
              'behaviour before 2026-08-21. Normal before a season\'s first match '
              'finishes; if it persists into a played gameweek the feed has changed.',
              file=sys.stderr)

    total_rows = total_unmatched = 0
    synced = []
    degraded = []      # written without a shot split, deliberately
    blocked = []       # not written at all, to protect rows already scored correctly
    dead_stats = []    # scoring stats the feed published empty — loud, but not fatal
    for gw in gws:
        pms, pgs, shots, shot_status = gw_data[gw]
        if not pms:
            print(f'  gw{gw}: no player match stats published yet, skipped')
            continue
        # Every run, not just under --audit. See COMMON_SCORING_STATS.
        dead_stats.extend((gw, c) for c in check_stat_coverage(gw, pms, cfg))
        ci_matches = fetch_csv_or_none(
            ci_url(ci_season, 'By Tournament', 'Premier League', f'GW{gw}', 'matches.csv'))

        # Only matches playermatchstats has published are in scope, for the shots feed
        # as well. See summarise_shots() for why the two must agree on WHICH matches.
        pms_match_ids = {r.get('match_id') for r in pms if r.get('match_id')}

        # A FINISHED MATCH WITH NO PLAYER ROWS AT ALL.
        #
        # The provider publishes a gameweek match by match, and matches.csv can
        # mark a match finished hours before its playermatchstats rows appear.
        # 2026/27 GW3 did exactly that: Hull 0-0 Villa was finished with a score,
        # and not one of the ~22 players who played in it had a row — so every
        # Hull and Villa player scored nothing for the round.
        #
        # Nothing downstream is CORRUPTED by this. price_gameweek's
        # last_complete_gw() refuses a gameweek where any club is below
        # MIN_PLAYERS_PER_CLUB, so no price can be computed off half a round, and
        # the pipeline is a pure replay, so the next run writes the rows in and
        # the points appear with no manual repair.
        #
        # What was missing was anyone SAYING SO. The shots feed had this warning
        # and player stats did not, so the gap was invisible until a player was
        # noticed missing by hand. Loud, by fixture, every run.
        if ci_matches:
            silent = finished_without_stats(ci_matches, pms_match_ids, team_by_code)
            if silent:
                print(f'  !! gw{gw}: {len(silent)} finished match(es) have NO player '
                      f'stats published — {", ".join(silent)}. Every player '
                      f'in them scores nothing for this gameweek until the provider '
                      f'catches up. Pricing is protected (last_complete_gw refuses a '
                      f'gameweek with a club below its minimum) and the next run '
                      f'repairs the points by itself.', file=sys.stderr)

        shot_extra, shot_note = {}, ''
        if shots:
            exact, tm, gi, go, ri, ro, off = reconcile_shot_box(
                shots, ci_matches, pms_match_ids)
            # Misclassified shots as a share of all shots the feed published for
            # the matches being scored. See SHOT_BOX_MAX_SHOT_ERROR for why this
            # is measured per shot rather than per team-match.
            classified = gi + go
            err = (off / classified) if classified else None
            if err is not None and err > SHOT_BOX_MAX_SHOT_ERROR:
                print(f'  !! gw{gw}: {off} of {classified} shots ({err * 100:.0f}%) '
                      f'are classified inside/outside the box differently from the '
                      f'feed\'s own team totals — we make it {gi} inside / {go} '
                      f'outside against their {ri} / {ro}, and only {exact}/{tm} '
                      f'team-matches agree exactly. The provider has probably '
                      f'rescaled start_x/start_y. Discarding the goal split for this '
                      f'gameweek — every goal scores 80 — and leaving SHOT_BOX_X to '
                      f'be recalibrated. Woodwork is unaffected.',
                      file=sys.stderr)
                shot_extra, _, _ = summarise_shots(shots, players, pms_match_ids)
                for e in shot_extra.values():
                    e['outside'] = 0
                degraded.append(gw)
            else:
                shot_extra, unresolved, unattributed = summarise_shots(
                    shots, players, pms_match_ids)
                if unresolved:
                    frac = unresolved / max(1, len(shots))
                    print(f'  {"!!" if frac > 0.05 else "!"} gw{gw}: {unresolved} shot '
                          f'row(s) ({frac * 100:.0f}%) carry a player_id that isn\'t in '
                          f'players.csv and were ignored. Above a few percent this '
                          f'means shots.csv has changed id space — check whether the '
                          f'ids need normalising (see pid_key).',
                          file=sys.stderr)
                if unattributed:
                    frac = unattributed / max(1, len(shots))
                    print(f'  ! gw{gw}: {unattributed} shot row(s) '
                          f'({frac * 100:.0f}%) were published with no player_id at '
                          f'all and cannot be credited. This is the feed\'s own gap, '
                          f'not an id-space change.', file=sys.stderr)
                # Coverage: a gameweek published match-by-match can have player stats
                # for a match whose shots aren't out yet. That is under-credit only
                # (the match-id filter above stops any over-credit), and the next
                # scheduled run repairs it — but say so rather than looking complete.
                shot_match_ids = {r.get('match_id') for r in shots} & pms_match_ids
                gap = len(pms_match_ids) - len(shot_match_ids)
                if gap > 0:
                    print(f'  ! gw{gw}: shots.csv covers {len(shot_match_ids)} of '
                          f'{len(pms_match_ids)} matches with player stats. Goals in '
                          f'the other {gap} scored at 80; the next run rescores them.',
                          file=sys.stderr)
                if err is not None:
                    shot_note = (f', box split {(1 - err) * 100:.0f}% of shots agree '
                                 f'with the feed ({exact}/{tm} team-matches exact)')
        elif not season_has_shots:
            degraded.append(gw)          # expected: the season has no shot feed yet
        else:
            reason = ('could not be fetched' if shot_status == 'error'
                      else 'is missing (404)')
            print(f'  !! gw{gw}: shots.csv {reason} but other gameweeks in this season '
                  f'have it. NOT writing this gameweek — doing so would replace '
                  f'correctly-split goals with flat 80s and wipe its woodwork. It will '
                  f'be picked up on the next run.', file=sys.stderr)
            blocked.append(gw)
            continue

        rows, unmatched = build_rows(gw, pms, pgs, players, team_by_id, team_by_code,
                                     fixture_index, ci_matches, shot_extra)
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
        loc = (f", {int(df['goal_outside_box'].sum())} outside-box goal(s), "
               f"{int(df['hit_woodwork'].sum())} woodwork{shot_note}" if shots
               else ', no shots.csv — every goal scored at the inside-box rate')
        if db is not None:
            db.upsert('gw_player_stats', gw_payload, 'gw,player_id')
            db.rpc('compute_all_gw_points', {'p_gw': gw})
            print(f'  gw{gw}: {len(scored)} players scored, written and recomputed'
                  + (f', {unmatched} unmatched' if unmatched else '') + loc)
        else:
            print(f'  gw{gw}: {len(scored)} players scored'
                  + (f', {unmatched} unmatched' if unmatched else '') + loc)

    # Only gameweeks that actually produced rows. Recomputing a gameweek with no
    # stats writes every manager a legitimate-looking zero — which is exactly what
    # `--gw 1-38` used to do for every future gameweek of the season.
    lines.append("-- Recompute every manager's points for the gameweeks just synced.")
    for gw in synced:
        lines.append(f'select public.compute_all_gw_points({gw});')
    lines.append('')

    if degraded:
        print(f'\n  ! Gameweek(s) {degraded} were scored without a goal-location split: '
              f'every goal at 80, no woodwork. Re-run once the feed publishes their '
              f'shots.csv.', file=sys.stderr)
    if dead_stats:
        cols = sorted({c for _g, c in dead_stats})
        print(f'\n  !! FEED GAP: {", ".join(cols)} came through empty for every player '
              f'in gameweek(s) {sorted({g for g, _c in dead_stats})}. These stats score '
              f'points and are not being awarded. This is not a crash and the rest of '
              f'the gameweek is correct, but it moves points unevenly between positions '
              f'and it feeds straight into pricing — do not leave it unexamined.',
              file=sys.stderr)
    if blocked:
        print(f'\n  !! Gameweek(s) {blocked} were NOT written — shots.csv was missing or '
              f'unreachable for them while other gameweeks this season have it. Their '
              f'existing rows in Supabase are untouched and still correct. Re-run; if '
              f'it repeats, check the feed.', file=sys.stderr)

    # A blocked gameweek is a real problem worth a red build: the feed has a shot
    # file for this season but not for that gameweek. A degraded one is not — it is
    # the normal state before a season's first match finishes.
    rc = 1 if blocked else 0

    if not total_rows:
        print('\nNo player rows produced — no points written.'
              + (' Fixtures were still updated.' if (db is not None and fixture_payload)
                 else ''))
        return rc

    if db is None:
        with open(out_path, 'w') as f:
            f.write('\n'.join(lines))
        print(f'\nWrote {out_path} — {total_rows} player-gameweek rows across '
              f'{len(synced)} gameweek(s)'
              + (f', {total_unmatched} unmatched' if total_unmatched else '') + '.')
        print('Paste it into the Supabase SQL Editor and Run.')
        return rc

    print(f'\nDone — {total_rows} player-gameweek rows across {len(synced)} gameweek(s)'
          + (f', {total_unmatched} unmatched' if total_unmatched else '')
          + ('  [dry run — nothing actually written]' if dry_run else '') + '.')
    return rc


if __name__ == '__main__':
    sys.exit(main() or 0)
