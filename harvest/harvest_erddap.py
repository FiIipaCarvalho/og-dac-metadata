"""Harvest glider dataset metadata from one ERDDAP: global attributes, flag variables, time span.

    python3.11 harvest_erddap.py IOOS --work DIR      (IOOS, VOTO, BODC or OTN)

Writes DIR/<source>_all.csv (the dataset list) and DIR/<source>_meta.jsonl (one line per glider dataset).
Resumable: datasets already in the .jsonl are skipped.
"""
import argparse
import concurrent.futures as cf
import csv
import io
import json
import os
import re

from common import ERDDAPS, FLAG_ATTRS, VAR_ATTRS, get


def keep(src, r):
    i, t = r['datasetID'], r['title'].lower()
    if i == 'allDatasets':
        return False
    if src == 'IOOS':
        return True
    if src == 'VOTO':
        return re.match(r'(nrt|delayed|OG_complete|OG_nrt)_SEA\d+_M\d+$', i) is not None
    if src == 'BODC':
        return any(w in t for w in ('glider', 'slocum', 'seaglider'))
    if src == 'OTN':
        return r['cdm_data_type'] in ('TrajectoryProfile', 'Trajectory') and 'test' not in i.lower()


def info(base, row):
    i = row['datasetID']
    try:
        t = get(f'{base}/info/{i}/index.csv')
    except Exception as e:
        return {'dataset_id': i, 'error': str(e)[:100]}
    g, names, qc, va = {}, [], {}, {}
    for l in csv.reader(io.StringIO(t)):
        if len(l) < 5:
            continue
        if l[0] == 'variable':
            names.append(l[1])
        elif l[0] == 'attribute' and l[1] == 'NC_GLOBAL':
            g[l[2]] = l[4][:300]
        elif l[0] == 'attribute' and l[1] == 'platform' and l[2] in ('wmo_id', 'type', 'id', 'long_name'):
            # IOOS Glider DAC keeps the WMO ID and glider type on the platform variable
            g['platform_' + l[2]] = l[4][:300]
        elif l[0] == 'attribute' and l[2] in FLAG_ATTRS:
            qc.setdefault(l[1], {})[l[2]] = l[4][:300]
        if l[0] == 'attribute' and l[1] != 'NC_GLOBAL' and l[2] in VAR_ATTRS:
            va.setdefault(l[1], {})[l[2]] = l[4][:200]
    # the dataset id goes in its own key: some ERDDAPs publish a global attribute called "id"
    return {'dataset_id': i, 'title': row['title'], 'cdm_data_type': row['cdm_data_type'],
            'min_time': row.get('minTime', ''), 'max_time': row.get('maxTime', ''),
            'globals': g, 'variables': names, 'qc': qc, 'var_attrs': va}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('source', choices=sorted(ERDDAPS))
    ap.add_argument('--work', required=True)
    a = ap.parse_args()
    base = ERDDAPS[a.source]
    lst = os.path.join(a.work, f'{a.source}_all.csv')
    if not os.path.exists(lst):
        open(lst, 'w').write(get(f'{base}/tabledap/allDatasets.csv?datasetID,title,institution,cdm_data_type,minTime,maxTime', timeout=300))
    rows = [r for r in list(csv.DictReader(open(lst)))[1:] if r['datasetID'] and keep(a.source, r)]
    out = os.path.join(a.work, f'{a.source}_meta.jsonl')
    done = {json.loads(l)['dataset_id'] for l in open(out)} if os.path.exists(out) else set()
    todo = [r for r in rows if r['datasetID'] not in done]
    print(a.source, 'datasets', len(rows), 'to fetch', len(todo), flush=True)
    with open(out, 'a') as f, cf.ThreadPoolExecutor(6) as ex:
        for d in ex.map(lambda r: info(base, r), todo):
            f.write(json.dumps(d) + '\n')
            f.flush()
    print(a.source, 'DONE', flush=True)


if __name__ == '__main__':
    main()
