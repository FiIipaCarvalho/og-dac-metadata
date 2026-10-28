"""Harvest IMOS ANFOG glider files from the IMOS S3 archive: one record per deployment folder.

    python3.11 harvest_imos.py --work DIR

Lists IMOS/ANFOG/ (paginated S3 listing), then reads the header of one file per deployment folder
(FV01 preferred) by HTTP byte range. Resumable.
"""
import argparse
import collections
import concurrent.futures as cf
import json
import os
import re
import urllib.parse

from common import IMOS_S3, get, h5_header, nc3_header


def list_keys():
    keys, tok = [], None
    while True:
        q = {'list-type': '2', 'prefix': 'IMOS/ANFOG/', 'max-keys': '1000'}
        if tok:
            q['continuation-token'] = tok
        x = get(IMOS_S3 + '?' + urllib.parse.urlencode(q), timeout=120)
        keys += re.findall(r'<Key>([^<]*)</Key>', x)
        m = re.search(r'<NextContinuationToken>([^<]*)</NextContinuationToken>', x)
        if not m:
            return keys
        tok = m.group(1)


def deployment(item):
    folder, ks = item
    k = sorted(ks, key=lambda x: ('FV01' not in x, x))[0]
    r = {'id': folder, 'file': k, 'nfiles': len(ks)}
    try:
        try:
            r['globals'], names, r['qc'], _ = h5_header(IMOS_S3 + k)
        except OSError:
            r['globals'], names, r['qc'], extra = nc3_header(IMOS_S3 + k)
            r.update(extra)
        r['n_vars'] = len(names)
    except Exception as e:
        r['error'] = str(e)[:100]
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True)
    a = ap.parse_args()
    kf = os.path.join(a.work, 'IMOS_keys.json')
    if not os.path.exists(kf):
        json.dump(list_keys(), open(kf, 'w'))
    dep = collections.defaultdict(list)
    for k in json.load(open(kf)):
        k = k[0] if isinstance(k, list) else k
        if k.endswith('.nc'):
            dep['/'.join(k.split('/')[:-1])].append(k)
    out = os.path.join(a.work, 'IMOS_meta.jsonl')
    done = {json.loads(l)['id'] for l in open(out)} if os.path.exists(out) else set()
    todo = [x for x in dep.items() if x[0] not in done]
    print('IMOS deployments', len(dep), 'to fetch', len(todo), flush=True)
    with open(out, 'a') as f, cf.ProcessPoolExecutor(8) as ex:
        for r in ex.map(deployment, todo, chunksize=2):
            f.write(json.dumps(r) + '\n')
            f.flush()
    print('IMOS DONE', flush=True)


if __name__ == '__main__':
    main()
