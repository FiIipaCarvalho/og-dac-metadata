"""Read the local track store: deployments metadata plus decimated tracks."""
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
WORK, STORE = ROOT / 'work', ROOT / 'store'
DAC_COLORS = {'IOOS': '#2f6fd0', 'VOTO': '#e0752d', 'BODC': '#2a9d8f',
              'OTN': '#5b8c3a', 'Coriolis': '#7c5cc4', 'IMOS': '#b5447e'}


def safe(dep):
    return re.sub(r'[^A-Za-z0-9._-]+', '__', dep)


def load_deployments():
    d = pd.read_csv(WORK / 'glider_deployments.csv', dtype={'deployment': str})
    d['dep_id'] = d['source'] + ':' + d['deployment']
    log = {}
    p = STORE / 'fetch_log.jsonl'
    if p.exists():
        for l in open(p):
            r = json.loads(l)
            log[r['dep_id']] = r
    d['track_status'] = d['dep_id'].map(lambda k: log.get(k, {}).get('status'))
    d['decimation'] = d['dep_id'].map(lambda k: log.get(k, {}).get('decimation'))
    return d


def load_tracks(since=None, until=None, sources=None):
    """Tracks for deployments active in [since, until], joined with key metadata.

    Catalogue dates pick the candidates; a deployment is kept only if its track has points
    in the window (catalogue dates can be wrong, e.g. BODC:623 lists 1970 to 2027 for 2024 data).
    """
    d = load_deployments()
    act = d['end_date'].fillna(d['start_date'])
    if since:
        d = d[act >= since]
    if until:
        d = d[d['start_date'] <= until]
    if sources:
        d = d[d['source'].isin(sources)]
    out = []
    for r in d.itertuples():
        p = STORE / 'tracks' / f'source={r.source}' / f'{safe(r.deployment)}.parquet'
        if not p.exists():
            continue
        t = pd.read_parquet(p)
        if (since and t['time'].max() < pd.Timestamp(since, tz='UTC')) or \
                (until and t['time'].min() > pd.Timestamp(until, tz='UTC')):
            continue
        t['dep_id'], t['source'] = r.dep_id, r.source
        t['tagged'], t['og1'], t['format'] = bool(r.tagged), bool(r.og1_available), r.format
        out.append(t)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()
