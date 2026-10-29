"""Per-file audit trail: build.py's decisions per file plus what the track fetch did with it.

    python3.11 file_audit.py [--out ../data/file_audit.csv]

Reads work/file_audit.csv (one row per file or ERDDAP dataset: stream and QC-level rules with the
attribute text they matched, format, tag, QC flag scheme) and store/fetch_log.jsonl (latest attempt
per deployment), and adds: used_for_track, track_status, track_decimation, track_points,
track_points_dropped, track_message. n_variables comes from the harvest (ERDDAP datasets) or, for
files the harvest only read headers of (Coriolis, IMOS, BODC file collections), from the variables
the track fetch found when it opened the file (store/var_attrs.jsonl); blank when unknown.
n_variables_from says which. Sorted by data centre, deployment, file.
"""
import argparse
import json

import pandas as pd

from store import STORE, WORK


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='file_audit.csv')
    a = ap.parse_args()
    au = pd.read_csv(WORK / 'file_audit.csv', dtype=str, keep_default_na=False)
    log = {}
    for l in open(STORE / 'fetch_log.jsonl'):
        r = json.loads(l)
        log[r['dep_id']] = r
    va = {}
    for l in open(STORE / 'var_attrs.jsonl'):
        r = json.loads(l)
        va[(r['dep_id'], r['ref'])] = len(r['variables'])
    from_fetch = au.apply(lambda r: va.get((r['dep_id'], r['file_or_dataset'])), axis=1)
    harvested = au['n_variables'].astype(int) > 0
    au['n_variables_from'] = ['harvest' if h else 'track fetch' if pd.notna(f) else '' for h, f in zip(harvested, from_fetch)]
    au['n_variables'] = [n if h else (str(int(f)) if pd.notna(f) else '') for n, h, f in zip(au['n_variables'], harvested, from_fetch)]
    g = lambda k: au['dep_id'].map(lambda d: log.get(d, {}).get(k, ''))
    used = au.apply(lambda r: log.get(r['dep_id'], {}).get('ref') == r['file_or_dataset'], axis=1)
    au['used_for_track'] = used.map({True: 'yes', False: ''})
    au['track_status'] = g('status').fillna('')
    au['track_decimation'] = g('decimation').where(used, '')
    au['track_points'] = g('n_points').where(used, '')
    au['track_points_dropped'] = g('n_dropped').where(used, '')
    au['track_message'] = g('message').map(lambda m: str(m or '')[:300])
    au.sort_values(['source', 'deployment', 'file_or_dataset']).to_csv(a.out, index=False)
    print(a.out, len(au), 'files,', int(used.sum()), 'used for tracks')


if __name__ == '__main__':
    main()
