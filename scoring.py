"""
Configurable scoring engine.

Every point value lives in scoring_config.json, never in code. A value is either a
number (same for all positions) or an object keyed by GK/DEF/MID/FWD.

Usage:
    from scoring import load_config, score_frame
    cfg = load_config('scoring_config.json')
    df  = score_frame(df, cfg)          # adds 'score' + one column per stat contribution
"""
import json
import numpy as np
import pandas as pd

POS = {'Goalkeeper': 'GK', 'Defender': 'DEF', 'Midfielder': 'MID', 'Forward': 'FWD'}


def derive(df: pd.DataFrame) -> pd.DataFrame:
    """Map raw feed columns onto the scoring vocabulary. One place to change when
    the upstream provider changes."""
    d = df.copy()
    d['pos'] = d['position'].map(POS)
    z = lambda c: d[c].fillna(0) if c in d.columns else 0.0

    out = pd.DataFrame(index=d.index)
    out['pos'] = d['pos']
    out['player_id'] = d['player_id']
    out['web_name'] = d['web_name']
    out['team'] = d['team']
    out['gw'] = d['gw']
    out['match_id'] = d['match_id']
    out['minutes'] = z('minutes_played')

    # --- attacking ---
    out['goal_inside_box']   = z('goal_inside_box')
    out['goal_outside_box']  = z('goal_outside_box')
    out['goal_penalty']      = z('goal_penalty')
    out['assist']            = z('assists')
    out['chance_created']    = z('chances_created')      # = key pass
    out['shot_on_target']    = z('shots_on_target') - z('goal_inside_box') - z('goal_outside_box') - z('goal_penalty')
    out['shot_on_target']    = out['shot_on_target'].clip(lower=0)   # non-scoring SoT only
    out['touch_opp_box']     = z('touches_opposition_box')
    out['successful_dribble']= z('successful_dribbles')  # take-ons; same stat, counted once
    out['accurate_cross']    = z('accurate_crosses')
    out['final_third_pass']  = z('final_third_passes')
    out['was_fouled']        = z('was_fouled')
    out['hit_woodwork']      = z('hit_woodwork')

    # --- defending / duels ---
    out['defcon_cbit']       = z('tackles') + z('interceptions') + z('blocks') + z('clearances')
    out['recovery']          = z('recoveries')
    out['aerial_won']        = z('aerial_duels_won')
    out['ground_duel_won']   = z('ground_duels_won')
    out['clean_sheet']       = z('clean_sheets')
    out['goal_conceded']     = z('goals_conceded_fpl')

    # --- goalkeeping ---
    out['save']              = z('saves')
    out['save_inside_box']   = z('saves_inside_box')
    out['goals_prevented']   = z('goals_prevented')      # xGOT faced minus goals conceded
    out['sweeper_action']    = z('sweeper_actions')
    out['high_claim']        = z('high_claim')
    out['penalty_saved']     = z('penalties_saved')

    # --- negative ---
    out['dispossessed']      = z('dispossessed')
    out['dribbled_past']     = z('dribbled_past')
    out['foul_committed']    = z('fouls_committed')
    out['offside']           = z('offsides')
    out['yellow']            = z('yellow_cards')
    out['red']               = z('red_cards')
    out['own_goal']          = z('own_goals')
    out['penalty_missed']    = z('penalties_missed') + z('penalties_missed_fpl')

    # --- appearance ---
    out['played']            = (out['minutes'] > 0).astype(float)
    out['played_60']         = (out['minutes'] >= 60).astype(float)
    # minutes-proportional alternative to flat appearance points: one unit per 10 minutes
    out['minutes_10']        = (out['minutes'] // 10).clip(upper=9)
    return out


def load_config(path):
    with open(path) as f:
        return json.load(f)


def _rate(val, pos_series):
    if isinstance(val, dict):
        return pos_series.map(val).fillna(0).astype(float)
    return float(val)


def score_frame(raw: pd.DataFrame, cfg: dict, keep_components=False):
    d = derive(raw)
    scale = cfg.get('scale', 1.0)
    total = pd.Series(0.0, index=d.index)
    comps = {}
    for stat, val in cfg['stats'].items():
        if stat not in d.columns:
            raise KeyError(f'config references unknown stat: {stat}')
        c = d[stat].astype(float) * _rate(val, d['pos'])
        comps[stat] = c
        total = total + c
    d['score'] = (total * scale).round(1)
    if keep_components:
        for k, v in comps.items():
            d['pts_' + k] = (v * scale).round(2)
    return d
