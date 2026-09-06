#!/usr/bin/env python3
"""
In-season price movement job.

Runs AFTER sync_gameweek.py in the same GitHub Action, for the reason Chris chose
Actions over an Edge Function in the first place: one implementation of the rules,
in one language, tested once.

    python3 price_gameweek.py --push              # price every played gameweek
    python3 price_gameweek.py --push --up-to 5    # as at the end of GW5
    python3 price_gameweek.py --dry-run           # serialise everything, write nothing
    python3 price_gameweek.py --report            # print the market, write nothing

WHAT IT DOES
------------
Reads `players_master` (opening prices) and every `gw_player_stats` row, replays
the whole season through `pricing_engine.replay()`, and writes back:

  players_master.price          the new price
  players_master.breakeven      the score needed in the player's NEXT appearance
  players_master.form_avg       his weighted rolling average
  players_master.appearances    appearances this season
  players_master.priced_to_gw   the gameweek this reflects
  player_price_history          one row per (gw, player) appearance, for the
                                movement chart and so a bad run can be audited

IDEMPOTENCY
-----------
Every price is recomputed from `opening_price` forward, never from the price
currently stored. Running the job twice, or ten times, on the same gameweek
produces byte-identical output — which is what makes it safe on the twice-daily
`--latest 3` schedule. See the long note in pricing_engine.py.

THE ONE THING THAT MUST NEVER BREAK
-----------------------------------
`opening_price` is the seed of the entire replay. If it is ever overwritten with
a moved price, every historical price silently changes. This job never writes it,
and refuses to start if any player is missing one.
"""
import json
import os
import sys

import pricing_engine as E
from sync_gameweek import Supabase

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_market(db):
    """players_master -> {id: opening_price}, plus names for reporting.

    `club_code` is carried too: last_complete_gw() needs a player -> club map to
    tell "the whole round is in" from "half the round is in" when fixture links
    are missing. See the note there.
    """
    rows = db.select('players_master',
                     'id,name,price,opening_price,left_pl,club_code')
    if not rows:
        raise RuntimeError(
            'players_master is empty. Run players_seed.sql before pricing.')
    missing = [r for r in rows if r.get('opening_price') is None]
    if missing:
        raise RuntimeError(
            f'{len(missing)} of {len(rows)} players have no opening_price '
            f'(e.g. id {missing[0]["id"]}, {missing[0].get("name")}). The replay '
            f'seeds from opening_price, so pricing without it would reprice those '
            f'players off a already-moved figure and compound every run. Apply '
            f'pricing-schema.sql, which backfills opening_price from price, before '
            f'running this job.')
    opening = {int(r['id']): float(r['opening_price']) for r in rows}
    names = {int(r['id']): r.get('name') or str(r['id']) for r in rows}
    current = {int(r['id']): float(r['price']) for r in rows}
    clubs = {int(r['id']): (r.get('club_code') or '').strip().upper()
             for r in rows if r.get('club_code')}
    return opening, names, current, clubs


def load_stats(db):
    """gw_player_stats -> {gw: {player_id: (score, minutes)}}"""
    rows = db.select('gw_player_stats', 'gw,player_id,points,minutes')
    out = {}
    for r in rows:
        gw = int(r['gw'])
        out.setdefault(gw, {})[int(r['player_id'])] = (
            float(r['points'] or 0), int(r['minutes'] or 0))
    return out, len(rows)


# A club that has genuinely played publishes a full matchday squad. Requiring
# only 8 players with minutes leaves room for the handful of players the feed
# scores who are absent from players_master (32 of them in GW1, one of whom was
# the round's top scorer) without ever accepting a club that did not play.
MIN_PLAYERS_PER_CLUB = 8


def last_complete_gw(db, stats, clubs=None):
    """The highest gameweek whose matches have all finished AND all been scored.

    WHY NOT SIMPLY "the latest gameweek with any stats":
    The sync publishes each match as the feed releases it, so on a Saturday
    afternoon a gameweek is half scored. Repricing then would anchor the magic
    number on whoever happened to play early, and a manager would watch prices
    move while matches were still going on. AFL — which this game copies — moves
    prices when the round is done.

    A gameweek is complete when every fixture is flagged finished AND every match
    in it has actually been scored. Establishing the second half has two paths:

      1. Fixture links intact — count distinct fixture_ids against the fixture
         count. Cheap and exact.

      2. Some rows have a null fixture_id — which sync_gameweek.py writes
         deliberately when the FPL fixtures mirror is unavailable, so this is a
         real state, not a theoretical one. Distinct-fixture counting is then
         meaningless, so completeness is established from CLUB COVERAGE instead:
         every club playing in that gameweek must have at least
         MIN_PLAYERS_PER_CLUB players with minutes on the board.

    WHY PATH 2 IS NOT "just accept it" (which is what this did until 2026-09-06):
    the old rule accepted any gameweek with at least one row the moment a single
    null fixture_id appeared. Reproduced: 10 fixtures all flagged finished, ONE
    player row, every link null — reported complete, which would have repriced
    the entire league off one row and anchored the magic number on it. All ten
    fixtures being flagged finished while stats are still arriving is the normal
    state for an hour or two every Sunday evening, because `finished` comes from
    the results overlay and player stats lag it.

    Club coverage also handles the double-gameweek case the old comment claimed
    to: gw_player_stats holds one row per (gw, player), so a player who played
    twice still appears once — but his club is still covered, so the gameweek is
    still correctly seen as complete.

    `clubs` is {player_id: club_code} from load_market(). Without it, path 2
    cannot be evaluated and the gameweek is refused rather than guessed at —
    prices moving late is harmless, prices moving on half a round is not.
    """
    fixtures = db.select('fixtures', 'id,gw,finished,home_club,away_club')
    if not fixtures:
        return None
    linked = db.select('gw_player_stats', 'gw,fixture_id,player_id,minutes')
    clubs = clubs or {}

    def blank():
        return {'total': 0, 'finished': 0, 'scored': set(), 'rows': 0,
                'unlinked': 0, 'clubs_playing': set(), 'clubs_seen': {}}

    by_gw = {}
    for f in fixtures:
        g = int(f['gw'])
        t = by_gw.setdefault(g, blank())
        t['total'] += 1
        if f.get('finished'):
            t['finished'] += 1
        for side in ('home_club', 'away_club'):
            c = (f.get(side) or '').strip().upper()
            if c:
                t['clubs_playing'].add(c)
    for r in linked:
        g = int(r['gw'])
        t = by_gw.setdefault(g, blank())
        t['rows'] += 1
        if r.get('fixture_id') is None:
            t['unlinked'] += 1
        else:
            t['scored'].add(r['fixture_id'])
        try:
            played = int(r.get('minutes') or 0) > 0
        except (TypeError, ValueError):
            played = False
        if played:
            c = clubs.get(int(r['player_id']))
            if c:
                t['clubs_seen'][c] = t['clubs_seen'].get(c, 0) + 1

    best = None
    for g in sorted(by_gw):
        t = by_gw[g]
        if t['total'] == 0 or t['finished'] < t['total'] or t['rows'] == 0:
            continue
        if t['unlinked'] == 0:
            complete = len(t['scored']) >= t['total']
        elif not t['clubs_playing'] or not clubs:
            print(f'  gw{g}: {t["unlinked"]} stat row(s) have no fixture link and '
                  f'there is no club map to fall back on, so whether the round is '
                  f'complete cannot be established. Not pricing it.', file=sys.stderr)
            complete = False
        else:
            missing = [c for c in sorted(t['clubs_playing'])
                       if t['clubs_seen'].get(c, 0) < MIN_PLAYERS_PER_CLUB]
            complete = not missing
            if missing:
                print(f'  gw{g}: fixture links are missing, and {len(missing)} of '
                      f'{len(t["clubs_playing"])} clubs have no squad published yet '
                      f'({", ".join(missing[:6])}). The round is not fully scored — '
                      f'not pricing it.', file=sys.stderr)
        if complete:
            best = g
    return best


def main():
    args = sys.argv[1:]
    push = '--push' in args
    dry = '--dry-run' in args
    report = '--report' in args
    up_to = None
    if '--up-to' in args:
        up_to = int(args[args.index('--up-to') + 1])

    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_KEY')
    if not url or not key:
        print('SUPABASE_URL and SUPABASE_SERVICE_KEY must be set.', file=sys.stderr)
        return 2
    db = Supabase(url, key, dry_run=dry or (not push))

    opening, names, current, clubs = load_market(db)
    stats, n_rows = load_stats(db)
    if not stats:
        print('No gameweek has been scored yet — no prices to move.')
        return 0

    played = sorted(stats)
    if up_to is not None:
        target_gw = up_to
    else:
        target_gw = last_complete_gw(db, stats, clubs)
        if target_gw is None:
            print(f'  Gameweek {played[-1]} is still being scored — no gameweek has '
                  f'every match finished and published yet. Prices move when a '
                  f'gameweek is complete, not part-way through it. Nothing to do.')
            return 0
        if target_gw < played[-1]:
            print(f'  GW{played[-1]} is still incomplete; pricing as at GW{target_gw}, '
                  f'the last finished gameweek.')
    print(f'{len(opening)} players priced · {n_rows} scored rows across '
          f'gameweeks {played[0]}-{played[-1]} · pricing as at GW{target_gw}')

    state, history, mn_track, unknown = E.replay(opening, stats, target_gw)

    # A player the feed scored but the game does not carry cannot be owned and
    # must never quietly acquire a price. Report it loudly — one of these was the
    # top scorer of GW1.
    if unknown:
        print(f'  !! {len(unknown)} player(s) have scored rows but are absent from '
              f'players_master, so they are unpriced and unpickable. Refresh the '
              f'player list. ids: {sorted(unknown)[:12]}'
              + (' ...' if len(unknown) > 12 else ''), file=sys.stderr)

    if target_gw < E.LOCK_UNTIL_GW:
        print(f'  GW{target_gw} is inside the lock (prices are frozen until '
              f'GW{E.LOCK_UNTIL_GW}). Breakevens are still written so the site can '
              f'show what everyone is playing for.')

    for gw, mn, pool, anchored in mn_track[1:]:
        print(f'  gw{gw}: magic number re-anchored to {mn:.5f} on '
              f'{pool} established starters')
    if len(mn_track) == 1:
        print(f'  magic number held at {E.OPENING_MN:.5f} — no pool of established '
              f'starters yet (needs {E.MN_MIN_POOL}; available at roughly GW9)')

    moves = [(state[p]['price'] - opening[p], p) for p in state]
    up = sum(1 for d, _ in moves if d > 0.001)
    down = sum(1 for d, _ in moves if d < -0.001)
    print(f'  {up} up, {down} down, {len(moves) - up - down} unchanged since the '
          f'opening market')

    if report or dry:
        moves.sort(reverse=True)
        print('\n  Biggest risers:')
        for d, p in moves[:10]:
            if d <= 0.001:
                break
            print(f'    {names.get(p, p):<18} £{opening[p]:>5.2f}m -> '
                  f'£{state[p]["price"]:>5.2f}m  ({d:+.2f})  '
                  f'avg {state[p]["form_avg"] or 0:>5.1f} over '
                  f'{state[p]["appearances"]} app(s)')
        print('  Biggest fallers:')
        for d, p in moves[-10:][::-1]:
            if d >= -0.001:
                break
            print(f'    {names.get(p, p):<18} £{opening[p]:>5.2f}m -> '
                  f'£{state[p]["price"]:>5.2f}m  ({d:+.2f})  '
                  f'avg {state[p]["form_avg"] or 0:>5.1f} over '
                  f'{state[p]["appearances"]} app(s)')

    if report and not push:
        return 0

    # ---- write back ---------------------------------------------------------
    def num(v):
        # PostgREST rejects NaN/Infinity, and json.dumps emits them bare. Any
        # non-finite value here is a bug upstream; turn it into a null rather
        # than a 400 halfway through a batch.
        if v is None:
            return None
        v = float(v)
        return round(v, 4) if v == v and abs(v) != float('inf') else None

    player_rows = []
    for pid, s in state.items():
        player_rows.append({
            'id': pid,
            'price': num(s['price']),
            'breakeven': num(s['breakeven']),
            'form_avg': num(s['form_avg']),
            'appearances': s['appearances'],
            'priced_to_gw': target_gw,
        })

    hist_rows = [{
        'gw': h['gw'], 'player_id': h['player_id'],
        'score': num(h['score']), 'minutes': h['minutes'],
        'price_before': num(h['price_before']), 'price_after': num(h['price_after']),
        'breakeven': num(h['breakeven']), 'form_avg': num(h['form_avg']),
        'magic_number': num(h['magic_number']), 'locked': bool(h['locked']),
    } for h in history]

    # Written through one RPC rather than two PostgREST upserts, for two reasons.
    #
    # 1. A PostgREST upsert is an INSERT ... ON CONFLICT underneath, so it must
    #    satisfy every NOT NULL column even when the row already exists.
    #    players_master.name and .pos are NOT NULL with no default, so an upsert
    #    carrying only the pricing columns fails outright — and carrying name and
    #    pos would mean this job could overwrite identity data it has no business
    #    touching.
    # 2. Atomicity. A market half-repriced — some players moved, some not, history
    #    disagreeing with prices — is worse than one not repriced at all, and is
    #    exactly the state a mid-batch failure would leave behind.
    #
    # apply_prices() also refuses to write opening_price, so the seed of the
    # replay cannot be corrupted even by a bug in this file.
    payload = {'p_players': player_rows, 'p_history': hist_rows, 'p_gw': target_gw}
    if db.dry_run:
        json.dumps(payload, allow_nan=False)     # prove it is postable
        print(f'  would write {len(player_rows)} player price row(s) and '
              f'{len(hist_rows)} history row(s)')
        return 0
    body = db.rpc('apply_prices', payload)
    print(f'  wrote {len(player_rows)} player price row(s) and '
          f'{len(hist_rows)} history row(s)'
          + (f' — server said {body.strip()}' if body and body.strip() else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main() or 0)
