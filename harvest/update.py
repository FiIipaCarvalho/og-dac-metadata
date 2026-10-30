"""Refresh everything with one command: harvest, build, tracks, map data, page.

    python3.11 update.py [--skip-harvest] [--push]

1. Harvest all six data centres into a fresh run folder, work/runs/<today>/ (a full re-harvest:
   the harvesters skip what is already in their .jsonl, so reusing the old files would miss
   deployments whose end date moved). About an hour, mostly IMOS and Coriolis.
2. build.py there, then copy the run's files over work/ and the CSV to data/.
3. fetch_tracks.py for all years (incremental: only new deployments, changed end dates, errors).
4. analysis/export_tracks.py --until <today>, analysis/file_audit.py (per-file audit trail), make_standalone.py.
5. Print what changed per data centre (records, tagged, OG1, recovered, DM) against the previous CSV and
   against data/findings_snapshot.csv, the figures as they were when the findings text was last reviewed
   (refresh that file with --mark-reviewed after re-reading the findings).
   --push commits and pushes data/, docs/ and the dashboard; otherwise review and commit by hand.
"""
import argparse
import datetime
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
FIELDS = ['tagged', 'og1_available', 'recovered', 'dm_any']


def run(*args, cwd=HERE):
    print('$', ' '.join(map(str, args)), flush=True)
    subprocess.run([PY, '-W', 'ignore', *map(str, args)], cwd=cwd, check=True)


def summary(csv):
    d = pd.read_csv(csv)
    return d.groupby('source').agg(records=('deployment', 'size'), **{f: (f, 'sum') for f in FIELDS})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--skip-harvest', action='store_true', help='reuse work/ as it is (tracks and page only)')
    ap.add_argument('--push', action='store_true')
    ap.add_argument('--mark-reviewed', action='store_true', help='only save the current figures as the reviewed snapshot')
    a = ap.parse_args()
    today = str(datetime.date.today())
    work = ROOT / 'work'
    snap = ROOT / 'data' / 'findings_snapshot.csv'
    if a.mark_reviewed:
        summary(ROOT / 'data' / 'glider_deployments.csv').to_csv(snap)
        print('saved', snap)
        return
    old = summary(ROOT / 'data' / 'glider_deployments.csv')

    if not a.skip_harvest:
        rd = work / 'runs' / today
        rd.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(4) as ex:  # the four ERDDAPs in parallel, as in the README
            list(ex.map(lambda s: run('harvest_erddap.py', s, '--work', rd), ['IOOS', 'VOTO', 'BODC', 'OTN']))
        for script in ('harvest_bodc_files.py', 'harvest_imos.py', 'harvest_coriolis.py', 'build.py'):
            run(script, '--work', rd)
        for f in rd.iterdir():
            if f.is_file():
                shutil.copy(f, work / f.name)
        shutil.copy(rd / 'glider_deployments.csv', ROOT / 'data' / 'glider_deployments.csv')

    run('fetch_tracks.py', '--work', work, '--store', ROOT / 'store', '--since', '1900-01-01', '--procs', '2')
    run('export_tracks.py', '--until', today, '--out', ROOT / 'data' / 'tracks.json', cwd=ROOT / 'analysis')
    run('file_audit.py', '--out', ROOT / 'data' / 'file_audit.csv', cwd=ROOT / 'analysis')
    run('make_standalone.py', cwd=ROOT)
    if not a.skip_harvest:  # README states the snapshot date
        import re
        rp = ROOT / 'README.md'
        rp.write_text(re.sub(r'\(snapshot [^)]*\)', f'(snapshot {today})', rp.read_text(), count=1))

    new = summary(ROOT / 'data' / 'glider_deployments.csv')
    diff = new.sub(old, fill_value=0).astype(int)
    print('\nChange since the previous CSV (new minus old):')
    print(diff[(diff != 0).any(axis=1)].to_string() if (diff != 0).any().any() else 'no change')
    if snap.exists():
        rev = new.sub(pd.read_csv(snap, index_col='source'), fill_value=0).astype(int)
        print('\nChange since the findings were last reviewed (data/findings_snapshot.csv):')
        print(rev[(rev != 0).any(axis=1)].to_string() if (rev != 0).any().any() else 'no change')
        print('Re-read the findings these numbers support, then run update.py --mark-reviewed.')

    if a.push:
        subprocess.run(['git', 'add', 'data', 'docs', 'dashboard', 'README.md'], cwd=ROOT, check=True)
        subprocess.run(['git', 'commit', '-m', f'Data refresh {today}'], cwd=ROOT, check=True)
        subprocess.run(['git', 'push'], cwd=ROOT, check=True)


if __name__ == '__main__':
    main()
