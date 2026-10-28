"""Harvest the Coriolis OceanGliders GDAC (data-oceangliders.ifremer.fr): one record per deployment folder.

    python3.11 harvest_coriolis.py --work DIR

Downloads the GDAC trajectory index (glider_traj_index.txt: time coverage, WMO, glider model, owner per
file), then reads each deployment's JSON metadata and the header of each NetCDF file by HTTP byte range
(global attributes, flag variables, platform variables).
netCDF4 is not thread-safe, so files are read in separate processes. Resumable.
"""
import argparse
import concurrent.futures as cf
import json
import os
import re

from common import CORIOLIS, get, remote_header


def ls(url):
    try:
        return re.findall(r'href="([^"?/][^"?]*)"', get(url))
    except Exception:
        return []


def folders(glider):
    return [glider + d for d in ls(CORIOLIS + glider) if d.endswith('/')]


def deployment(folder):
    u = CORIOLIS + folder
    files = ls(u)
    r = {'id': folder.strip('/'), 'files': [f for f in files if not f.endswith('/')], 'nc': {}}
    for j in [f for f in files if f.endswith('.json')]:
        try:
            x = json.loads(get(u + j), strict=False)
        except Exception as e:
            r['json_err'] = str(e)[:80]
            continue
        if 'EGODeployment' in x:
            e = x['EGODeployment'][0]
            pi = (e.get('PLATFORMINSTANCE') or [{}])[0]
            pm = (e.get('PLATFORMMODEL') or [{}])[0]
            r.update(start=str(e.get('DEPLOYMENT_START_DATE', '')), project=e.get('PROJNAME', ''),
                     owner=pi.get('GLIDER_OWNER') or '', wmo=str(pi.get('WMO_PLATFORM_CODE') or ''),
                     platform_model=' '.join(str(pm.get(k, '')) for k in ('PLATFORM_MAKER', 'DDESC', 'MODEL_NAME')).strip())
        elif 'global_attributes' in x:
            ga = x['global_attributes']
            r.update(owner=r.get('owner') or ga.get('institution', ''), wmo=r.get('wmo') or ga.get('wmo_platform_code', ''),
                     json_network=ga.get('network', ''))
            gc = x.get('glider_characteristics') or {}
            if isinstance(gc, dict):
                r['platform_model'] = r.get('platform_model') or ' '.join(str(gc.get(k, '')) for k in ('platform_maker', 'platform_type')).strip()
    for n in [f for f in files if f.endswith('.nc')][:2]:
        try:
            g, names, qc, extra = remote_header(u + n, time_var=())
            r['nc'][n] = {'globals': g, 'qc': qc, 'n_vars': len(names), **extra}
        except Exception as e:
            r.setdefault('nc_err', []).append(f'{n}: {str(e)[:80]}')
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True)
    a = ap.parse_args()
    # the GDAC's own index: time coverage, WMO, glider model, owner, program, parameters for every file
    open(os.path.join(a.work, 'Coriolis_traj_index.txt'), 'w').write(get(CORIOLIS + 'glider_traj_index.txt', timeout=300))
    gliders = [g for g in ls(CORIOLIS) if g.endswith('/')]
    with cf.ThreadPoolExecutor(8) as ex:
        dep = [d for l in ex.map(folders, gliders) for d in l]
    out = os.path.join(a.work, 'Coriolis_meta.jsonl')
    # resume: keep records that read every file cleanly and got a time span (or had no NetCDF files)
    kept, done = [], set()
    if os.path.exists(out):
        for l in open(out):
            r = json.loads(l)
            if not r.get('nc_err'):  # times now come from the GDAC trajectory index
                kept.append(l)
                done.add(r['id'])
        open(out, 'w').writelines(kept)
    todo = [d for d in dep if d.strip('/') not in done]
    print('Coriolis gliders', len(gliders), 'deployments', len(dep), 'to fetch', len(todo), flush=True)
    with open(out, 'a') as f, cf.ProcessPoolExecutor(4) as ex:
        for fut in cf.as_completed([ex.submit(deployment, d) for d in todo]):
            f.write(json.dumps(fut.result()) + '\n')
            f.flush()
    print('Coriolis DONE', flush=True)


if __name__ == '__main__':
    main()
