"""Download the source files behind ERDDAP datasets into the local store, for later analyses.

    python3.11 download_files.py --work ../work --store ../store --sources OTN [VOTO BODC] [--since 2021-01-01] [--dry-run]

Lists each dataset's files through ERDDAP's /files/ service (link names are HTML-entity encoded,
e.g. &#x5f; for "_") and downloads them to store/files/source=<DAC>/<dataset_id>/, keeping what is
already there (resumable). Two downloads at a time with back-off: OTN resets connections under
sustained load and answers each file in about 1.5 s, so a deployment of ~3,000 profile files takes
about an hour. IOOS refuses /files/ (403). --dry-run only counts files and bytes from the listings.
"""
import argparse
import csv
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from common import ERDDAPS, get


def listing(url, depth=0):
    """(relative path, size) of every file under an ERDDAP /files/ folder, one subfolder level deep."""
    page = html.unescape(get(url, timeout=300, tries=3))
    out = []
    for row in re.findall(r'<tr>(.*?)</tr>', page, re.S):
        m = re.search(r'href="([^"?]+)"', row)
        if not m or m.group(1).startswith(('http', '..', '/')):
            continue
        name = m.group(1)
        if name.endswith('/') and depth == 0:
            out += [(name + n, sz) for n, sz in listing(url + name, 1)]
        elif not name.endswith('/'):
            sz = re.findall(r'<td[^>]*>(\d+)</td>', row)
            out.append((name, int(sz[0]) if sz else 0))
    return out


def fetch(url, path):
    if os.path.exists(path):
        return 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for k in range(6):
        try:
            with urllib.request.urlopen(url, timeout=300) as r, open(path + '.part', 'wb') as f:
                f.write(r.read())
            os.replace(path + '.part', path)
            return 1
        except (OSError, urllib.error.URLError):
            if k == 5:
                raise
            time.sleep(5 * 2 ** k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True)
    ap.add_argument('--store', required=True)
    ap.add_argument('--sources', nargs='+', required=True)
    ap.add_argument('--since', default='1900-01-01', help='deployments active since (end date, else start date)')
    ap.add_argument('--threads', type=int, default=2)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    rows = {f"{r['source']}:{r['deployment']}": r for r in csv.DictReader(open(os.path.join(a.work, 'glider_deployments.csv')))}
    tot_files = tot_bytes = 0
    for line in open(os.path.join(a.work, 'deployment_refs.jsonl')):
        d = json.loads(line)
        r = rows.get(d['dep_id'])
        if d['source'] not in a.sources or not r or (r['end_date'] or r['start_date']) < a.since:
            continue
        for ref in d['refs']:
            if '/tabledap/' not in ref.get('url', ''):
                continue
            did = ref['id']
            base = f"{ERDDAPS[d['source']]}/files/{did}/"
            try:
                files = listing(base)
            except Exception as e:
                print(d['dep_id'], did, 'listing failed:', str(e)[:80], flush=True)
                continue
            n, b = len(files), sum(sz for _, sz in files)
            tot_files, tot_bytes = tot_files + n, tot_bytes + b
            if a.dry_run:
                print(d['dep_id'], did, n, 'files', round(b / 1e6, 1), 'MB', flush=True)
                continue
            dest = os.path.join(a.store, 'files', f"source={d['source']}", did)
            with ThreadPoolExecutor(a.threads) as ex:
                got = sum(ex.map(lambda f: fetch(base + f[0], os.path.join(dest, f[0])), files))
            print(d['dep_id'], did, n, 'files,', got, 'new', flush=True)
    print('total', tot_files, 'files', round(tot_bytes / 1e9, 2), 'GB')


if __name__ == '__main__':
    main()
