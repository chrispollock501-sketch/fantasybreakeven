"""
Breakeven pricing engine — the in-season rules, as pure functions.

WHY THIS FILE EXISTS SEPARATELY FROM pricing.py
------------------------------------------------
`pricing.py` is a 2025/26 *backtest* script, and it implements a rule set that
`game-rules.md` later superseded. It demonstrably did NOT produce the prices now
live in the game: priced on 60-minute starts only it makes Doku £15.86m, where
`game-rules.md` §5's every-appearance rule makes him £10.42m — and £10.42m is
what `out/site_data.json` actually carries. Three of its constants are therefore
wrong for in-season use:

  MIN_MINUTES = 60          -> every appearance counts (game-rules.md §5)
  anchor on len(form) >= 8  -> anchor on ESTABLISHED STARTERS, or fringe players'
                               cameo-depressed averages drag the anchor down and
                               inflate the whole market (game-rules.md §5)
  ABSENCE_DECAY*            -> dropped entirely; a player who isn't playing is
                               frozen at his last price, full stop (game-rules.md §3, §5)

`game-rules.md` and `calibration-results.md` are authoritative on the rules.
`pricing.py` is authoritative only on the *shape* of the formula, which is
reproduced here unchanged.

THE FORMULA
-----------
    w            = [0.2817, 0.2113, 0.1585, 0.1188, 0.0891, 0.0669, 0.0501, 0.0376]
                   (geometric, decay 0.75, most recent appearance first)
    new_average  = Σ wᵢ · scoreᵢ        over the last 8 APPEARANCES this season
    target       = MN × new_average
    new_price    = clip(price + clip(target − price, ±0.30), 4.00, 20.00)
    breakeven    = (price / MN − Σᵢ≥₁ wᵢ·scoreᵢ) / w₀

DESIGN: PURE REPLAY, NOT INCREMENTAL MUTATION
---------------------------------------------
`price_after(gw N)` is computed as a pure function of (opening price, every score
from GW1..N). It is never derived from the price currently in the database.

That is what makes the job safe to run on the twice-daily `--latest 3` schedule
the handover requires: re-running a gameweek cannot move a price twice, because
there is no accumulation to double. It also means a *revised* stat line — the
feed correcting a match days later — self-heals on the next run, where a
"last-priced-gameweek" flag would have frozen the error in permanently.

The price of that choice is that `players_master.opening_price` must be
preserved and must never be written by this job. `price_gameweek.py` refuses to
run if it is missing.
"""

# --- engine parameters --------------------------------------------------------
# Geometric, decay 0.75. calibration-results.md §3: an 8-match window with a
# ±£0.30m cap lands "beat your breakeven" near 50% with the smoothest price
# paths. A 3-match window makes prices thrash (thrash 0.141 -> 1.661 uncapped).
WEIGHTS = [0.2817, 0.2113, 0.1585, 0.1188, 0.0891, 0.0669, 0.0501, 0.0376]

# The PRICING ANCHOR, deliberately NOT the game's own squad rules. The game is
# 20 players at a £105m cap; the anchor is "15 median regulars cost £100m".
# game-rules.md §2 keeps these two levers separate on purpose — the magic number
# sets the price scale, the cap sets the difficulty. Do not "fix" this to 105/20.
CAP_ANCHOR = 100.0
ANCHOR_SQUAD = 15

FLOOR = 4.00
CEIL = 20.00
MAX_MOVE = 0.30                 # per gameweek, either direction
LOCK_UNTIL_GW = 3               # gw < this cannot move; GW3 is the first repricing

# Confirmed empirically against two live prices in the shipped market:
#   Ødegaard  £5.92m  / 50.8  = 0.11654
#   B.Fernandes £15.08m / 129.4 = 0.11654
# and it sits at the bottom of the 0.1165 -> 0.1248 band game-rules.md §5 reports
# for the corrected backtest.
OPENING_MN = 0.11654

# --- magic-number re-anchoring ------------------------------------------------
# calibration-results.md §3: league-wide scoring drifted 8.7% between seasons, and
# leaving MN fixed inflated the market (237 up vs 60 down, 62% beating breakeven).
# Re-anchoring each gameweek so the median regular sits at cap/squad keeps the
# economy zero-sum, which is what AFL does.
#
# WHAT COUNTS AS A "REGULAR" IS A REAL DECISION, NOT A DETAIL. game-rules.md §5
# says established starters — "15+ starts of 60+ minutes" — measured over a whole
# prior season. In-season that threshold cannot be met before GW15, so the direct
# translation would leave MN un-anchored for a third of the season and then jolt
# the entire market in one gameweek.
#
# The in-season analogue used here keeps the INTENT (exclude fringe players whose
# cameo-depressed averages would drag the anchor down) while becoming available
# at around GW9: a full 8-appearance window, of which at least 6 were 60+ minute
# outings. Until a pool of MN_MIN_POOL such players exists, MN holds at OPENING_MN.
#
# This is the one parameter chosen without an explicit instruction in game-rules.md.
# It cannot bite before ~GW9, so there is time to change it.
MN_WINDOW_MIN_60 = 6            # of the 8 appearances in the window
MN_MIN_POOL = 50                # below this the median is too noisy to anchor on


def rolling(hist, weights=WEIGHTS):
    """Weighted average over the most recent appearances, most recent first.

    `hist` is a list of scores. Weights are renormalised to however many
    appearances the player actually has, so a player with three games is
    averaged over three games rather than being diluted toward zero.
    """
    n = min(len(hist), len(weights))
    if n == 0:
        return None
    w = weights[:n]
    tot = sum(w)
    return sum(wi * si for wi, si in zip(w, hist[:n])) / tot


def breakeven(price, hist, mn, weights=WEIGHTS):
    """The score needed in the NEXT appearance to leave the price unchanged.

    `hist` must be the form BEFORE that appearance, most recent first. Derived by
    setting target == price and solving for score₀:

        price / mn = w₀·score₀ + Σᵢ≥₁ wᵢ·scoreᵢ

    At GW1 `hist` is empty, so n = 0, w = [1.0] after renormalisation, and the
    result collapses to exactly `price / mn` — a player's opening breakeven is
    his prior-season weighted average, with no approximation.

    This legitimately returns NEGATIVE numbers once a player's recent form is far
    above what his price implies (Ødegaard's is −24.4 after a 151). That is
    arithmetically correct and is shown to the user as-is, per Chris's decision.
    """
    if mn is None or mn <= 0:
        return None
    n = min(len(hist), len(weights) - 1)
    w = weights[:n + 1]
    tot = sum(w)
    w = [wi / tot for wi in w]
    known = sum(wi * si for wi, si in zip(w[1:], hist[:n])) if n else 0.0
    return (price / mn - known) / w[0]


def clip(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def is_established_starter(window):
    """`window` is the player's last-8 list of (score, minutes), most recent first.

    Established = a full window, mostly of real outings. See MN_WINDOW_MIN_60.
    """
    if len(window) < len(WEIGHTS):
        return False
    return sum(1 for _s, m in window if m >= 60) >= MN_WINDOW_MIN_60


def anchor_mn(forms, current_mn):
    """Re-anchor so the median established starter costs exactly cap ÷ squad.

    Returns (mn, pool_size, anchored). Holds `current_mn` when the pool is too
    small to take a stable median from — which is the normal state until ~GW9.
    """
    regs = []
    for window in forms.values():
        if is_established_starter(window):
            avg = rolling([s for s, _m in window])
            if avg and avg > 0:
                regs.append(avg)
    if len(regs) < MN_MIN_POOL:
        return current_mn, len(regs), False
    regs.sort()
    n = len(regs)
    median = regs[n // 2] if n % 2 else (regs[n // 2 - 1] + regs[n // 2]) / 2.0
    if median <= 0:
        return current_mn, len(regs), False
    return (CAP_ANCHOR / ANCHOR_SQUAD) / median, len(regs), True


def replay(opening_prices, stats_by_gw, up_to_gw,
           opening_mn=OPENING_MN, lock_until_gw=LOCK_UNTIL_GW,
           max_move=MAX_MOVE, floor=FLOOR, ceil=CEIL):
    """Replay every gameweek from opening prices and return the resulting market.

    opening_prices : {player_id: float}
    stats_by_gw    : {gw: {player_id: (score, minutes)}}
    up_to_gw       : replay gameweeks 1..up_to_gw inclusive

    Returns (state, history, mn_track) where

      state[pid]  = {'price', 'breakeven', 'form', 'appearances', 'form_avg', 'mn'}
      history     = list of per-(gw, player) movement records, appearances only
      mn_track    = [(gw, mn, pool_size, anchored)] each time it is recomputed

    An APPEARANCE is minutes > 0. game-rules.md §5: "prices move only when a
    player takes the field", and every appearance counts, cameos included — a
    sub-15-minute outing averages 5.8 points and *should* drag a rotation
    player's average down. A 0-minute row (an unused sub, which the feed does
    publish) is not an appearance and must not touch price or form.
    """
    price = dict(opening_prices)
    forms = {pid: [] for pid in opening_prices}     # (score, minutes), recent first
    mn = opening_mn
    history = []
    mn_track = [(0, mn, 0, False)]
    unknown = set()

    for gw in range(1, up_to_gw + 1):
        gw_rows = stats_by_gw.get(gw, {})
        appeared = []

        # 1. Record the breakeven each player was carrying INTO this gameweek,
        #    then fold the new score into his form. Both must happen before any
        #    price moves, so the recorded breakeven is the one he was actually
        #    playing against.
        for pid, (score, minutes) in gw_rows.items():
            if pid not in price:
                unknown.add(pid)
                continue
            if minutes <= 0:
                continue
            be_before = breakeven(price[pid], [s for s, _m in forms[pid]], mn)
            appeared.append((pid, score, minutes, price[pid], be_before))
            forms[pid].insert(0, (score, minutes))
            del forms[pid][len(WEIGHTS):]

        # 2. Nothing moves during the lock, but form still accumulates above, so
        #    GW3's first repricing already has three gameweeks of evidence behind
        #    it rather than starting from a blank slate.
        if gw < lock_until_gw:
            for pid, score, minutes, p_before, be_before in appeared:
                history.append(dict(gw=gw, player_id=pid, score=score, minutes=minutes,
                                    price_before=p_before, price_after=p_before,
                                    breakeven=be_before, moved=False, locked=True,
                                    magic_number=mn,
                                    form_avg=rolling([s for s, _m in forms[pid]])))
            continue

        # 3. Re-anchor before applying moves, so every player in this gameweek is
        #    priced on the same magic number.
        mn, pool, anchored = anchor_mn(forms, mn)
        if anchored:
            mn_track.append((gw, mn, pool, True))

        # 4. Move the price of everyone who took the field. Everyone else is
        #    frozen — no decay (game-rules.md §5). That also settles the
        #    departed-player case: frozen at his last price.
        for pid, score, minutes, p_before, be_before in appeared:
            avg = rolling([s for s, _m in forms[pid]])
            target = mn * avg
            move = clip(target - p_before, -max_move, max_move)
            p_after = round(clip(p_before + move, floor, ceil), 2)
            price[pid] = p_after
            history.append(dict(gw=gw, player_id=pid, score=score, minutes=minutes,
                                price_before=p_before, price_after=p_after,
                                breakeven=be_before, moved=p_after != p_before,
                                locked=False, magic_number=mn, form_avg=avg))

    state = {}
    for pid, p in price.items():
        window = forms[pid]
        scores = [s for s, _m in window]
        state[pid] = {
            'price': round(p, 2),
            # The breakeven a player carries NOW, for his next appearance.
            'breakeven': breakeven(p, scores, mn),
            'form': scores,
            'appearances': len(scores),
            'form_avg': rolling(scores),
            'mn': mn,
        }
    return state, history, mn_track, unknown
