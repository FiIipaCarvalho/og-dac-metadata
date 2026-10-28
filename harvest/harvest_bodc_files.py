"""Harvest BODC's OG1 file collections on its ERDDAP (real-time and recovery), one record per deployment folder.

    python3.11 harvest_bodc_files.py --work DIR

BODC's recovery (delayed) OG1 files are not separate ERDDAP datasets: they sit in the file collection
Public_OG1_Data_001_Recovery (real time: Public_OG1_Data_001). The server doesn't support byte ranges,
so headers can't be read without downloading whole files; each folder's EGO deployment JSON gives the
deployment ID, start date, owner, WMO ID and glider model instead.
"""
import argparse
import concurrent.futures as cf
import html
import json
import os
import re

from common import ERDDAPS, get

COLLECTIONS = {'Public_OG1_Data_001': 'R', 'Public_OG1_Data_001_Recovery': 'P'}


def ls(url):
    try:
        t = get(url)
    except Exception:
        return []
    return [h for h in (html.unescape(x) for x in re.findall(r'href="([^"?]*)"', t)) if not h.startswith(('http', '/', '..'))]


def folder(item):
    coll, f = item
    u = f"{ERDDAPS['BODC']}/files/{coll}/{f}"
    files = ls(u)
    r = {'collection': coll, 'mode': COLLECTIONS[coll], 'folder': f.strip('/'), 'files': files}
    for j in [x for x in files if x.endswith('.json')]:
        try:
            e = json.loads(get(u + j), strict=False)['EGODeployment'][0]
        except Exception as ex:
            r['json_err'] = str(ex)[:80]
            continue
        pi = (e.get('PLATFORMINSTANCE') or [{}])[0]
        pm = (e.get('PLATFORMMODEL') or [{}])[0]
        r.update(deployment_id=str(e.get('DEPLOYMENT_ID', '')), start=str(e.get('DEPLOYMENT_START_DATE', '')),
                 project=e.get('PROJNAME', ''), owner=pi.get('GLIDER_OWNER', ''), wmo=str(pi.get('WMO_PLATFORM_CODE') or ''),
                 platform_model=' '.join(str(pm.get(k, '')) for k in ('PLATFORM_MAKER', 'DDESC', 'MODEL_NAME')).strip())
    if not r.get('deployment_id'):
        m = re.search(r'_(\d{3,4})(_[RDP])?\.nc$', ' '.join(files))
        r['deployment_id'] = m.group(1) if m else ''
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True)
    a = ap.parse_args()
    items = [(c, f) for c in COLLECTIONS for f in ls(f"{ERDDAPS['BODC']}/files/{c}/") if f.endswith('/')]
    print('BODC file folders', len(items), flush=True)
    with open(os.path.join(a.work, 'BODC_files.jsonl'), 'w') as out, cf.ThreadPoolExecutor(6) as ex:
        for r in ex.map(folder, items):
            out.write(json.dumps(r) + '\n')
    print('BODC files DONE', flush=True)


if __name__ == '__main__':
    main()
