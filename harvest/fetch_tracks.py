"""Fetch decimated glider tracks (time, lat, lon) into the local store: one Parquet file per deployment.

    python3.11 fetch_tracks.py --work ../work --store ../store [--since 2025-10-28] [--sources IOOS VOTO] [--force]

Reads DIR/glider_deployments.csv and DIR/deployment_refs.jsonl (from build.py). For each deployment active
since --since (end date, or start date where there is none; default 12 months ago):
- ERDDAP data centres (IOOS, VOTO, BODC, OTN): the server averages to one point per dive/profile
  (orderByMean on dive_num/profile_id) or, without a dive variable, per hour.
- Files (Coriolis, IMOS, BODC file collections): only time/lat/lon are read (HDF5 by byte range, NetCDF3
  streamed, BODC downloaded to a temporary file and deleted); averaged to one point per hour.
Recovered datasets are tried before real-time ones. Every attempt is appended to STORE/fetch_log.jsonl and
variable names/attributes to STORE/var_attrs.jsonl. Incremental: a deployment is fetched again only when it
is new, its end date changed, or its last attempt failed (or with --force).
"""
import argparse
import concurrent.futures as cf
import csv
import datetime
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pandas as pd

from common import VAR_ATTRS, RangeFile, get, magic

ERDDAP_SOURCES = ('IOOS', 'VOTO', 'BODC', 'OTN')
DIVE_VARS = ('dive_num', 'divenum', 'dive_number', 'profile_id')  # first one present wins
MAX_SPEED = 3.0  # m/s between consecutive points; faster jumps are bad fixes


def safe(dep):
    return re.sub(r'[^A-Za-z0-9._-]+', '__', dep)


def track_path(store, source, dep):
    return os.path.join(store, 'tracks', f'source={source}', safe(dep) + '.parquet')


# ---------------- cleaning ----------------
def clean(df, t_min=None, t_max=None):
    """Drop fill/out-of-range positions, times outside the deployment window, and speed jumps."""
    df = df.dropna(subset=['time', 'lat', 'lon'])
    df = df[df.lat.between(-90, 90) & df.lon.between(-180, 360) & ~((df.lat == 0) & (df.lon == 0))]
    df = df[(df.time > pd.Timestamp('1990-01-01', tz='UTC')) & (df.time < pd.Timestamp.now(tz='UTC') + pd.Timedelta(days=1))]
    if t_min is not None:
        df = df[df.time >= t_min]
    if t_max is not None:
        df = df[df.time <= t_max]
    df = df.sort_values('time').reset_index(drop=True)
    df['lon'] = ((df.lon + 180) % 360) - 180
    if len(df) < 2:
        return df
    keep, last = [0], 0
    lat, lon, t = np.radians(df.lat.values), np.radians(df.lon.values), df.time.values.astype('datetime64[s]').astype(float)
    for i in range(1, len(df)):
        a = np.sin((lat[i] - lat[last]) / 2) ** 2 + np.cos(lat[i]) * np.cos(lat[last]) * np.sin((lon[i] - lon[last]) / 2) ** 2
        dist = 2 * 6371000 * np.arcsin(np.sqrt(a))
        if dist / max(t[i] - t[last], 1) <= MAX_SPEED or dist < 2000:
            keep.append(i)
            last = i
    return df.iloc[keep].reset_index(drop=True)


def hourly(df):
    df = df.set_index('time')[['lat', 'lon']].resample('1h').mean().dropna().reset_index()
    df['dive'] = np.nan
    return df


# ---------------- ERDDAP ----------------
def pick(names, *cands):
    low = {n.lower(): n for n in names}
    return next((low[c.lower()] for c in cands if c.lower() in low), None)


def erddap_track(ref, tries=3, timeout=600, window=None):
    names = ref['variables']
    t, la, lo = pick(names, 'time'), pick(names, 'latitude'), pick(names, 'longitude')
    if not (t and la and lo):
        raise ValueError('no time/latitude/longitude variables')
    d = pick(names, *DIVE_VARS)
    cols = f'{t},{la},{lo}' + (f',{d}' if d else '')
    tw = f'&{t}>={window[0]}&{t}<{window[1]}' if window else ''
    q = f'{cols}{tw}&{la}!=NaN&orderByMean("{d or t + "/1hour"}")'
    url = ref['url'] + '.csv?' + urllib.parse.quote(q, safe='&=,')
    txt = get(url, timeout=timeout, tries=tries)
    import io
    df = pd.read_csv(io.StringIO(txt), skiprows=[1])
    df = df.rename(columns={t: 'time', la: 'lat', lo: 'lon', **({d: 'dive'} if d else {})})
    df['time'] = pd.to_datetime(df.time, utc=True, errors='coerce')
    if 'dive' not in df:
        df['dive'] = np.nan
    return df[['time', 'lat', 'lon', 'dive']], ('dive' if d else 'hour')


def erddap_track_weekly(ref, start, end):
    """Week-by-week queries: ERDDAP opens only the files in each time window, so datasets that time out
    as a whole (OTN, one file per profile) answer in seconds per week."""
    parts, dec, t = [], 'dive', start.floor('D')
    while t <= end:
        w = (t.strftime('%Y-%m-%dT%H:%M:%SZ'), (t + pd.Timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%SZ'))
        try:
            df, dec = erddap_track(ref, tries=2, timeout=150, window=w)
            parts.append(df)
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = no data in that week
                raise
        t += pd.Timedelta(days=7)
    if not parts:
        raise ValueError('no data in any week')
    df = pd.concat(parts, ignore_index=True)
    return df.drop_duplicates(subset=['dive'] if df['dive'].notna().any() else None), dec


def otn_files_track(ref, cache, threads=2):
    """OTN fallback: its ERDDAP datasets aggregate one small IOOS-format file per profile, and older
    ones time out (90 s gateway) even for trivial queries. Download the per-profile files from
    ERDDAP's /files/ service (kept in cache/<dataset_id>/) and take each profile's time and position."""
    import html
    import netCDF4
    from concurrent.futures import ThreadPoolExecutor
    base, did = ref['url'].rsplit('/tabledap/', 1)
    listing = html.unescape(get(f'{base}/files/{did}/', timeout=300, tries=3))
    names = sorted(set(re.findall(r'href="([^"/?]+\.nc)"', listing)))
    if not names:
        raise ValueError('no files listed under /files/')
    d = os.path.join(cache, did)
    os.makedirs(d, exist_ok=True)
    def one(n):
        p = os.path.join(d, n)
        if not os.path.exists(p):  # OTN resets connections now and then: retry with back-off
            for k in range(6):
                try:
                    with urllib.request.urlopen(f'{base}/files/{did}/{n}', timeout=300) as r, open(p + '.part', 'wb') as f:
                        f.write(r.read())
                    os.replace(p + '.part', p)
                    break
                except (OSError, urllib.error.URLError):
                    if k == 5:
                        raise
                    time.sleep(5 * 2 ** k)
        return p
    def read(p):  # netCDF4 is not thread-safe: read one file at a time
        with netCDF4.Dataset(p) as ds:
            V = ds.variables
            if all(k in V for k in ('profile_time', 'profile_lat', 'profile_lon')):
                t, la, lo = (float(np.ma.filled(V[k][:], np.nan).ravel()[0]) for k in ('profile_time', 'profile_lat', 'profile_lon'))
                u = V['profile_time'].units
            else:
                t, la, lo = (float(np.nanmean(np.ma.filled(V[k][:], np.nan))) for k in ('time', 'lat', 'lon'))
                u = V['time'].units
            pid = float(np.ma.filled(V['profile_id'][:], np.nan).ravel()[0]) if 'profile_id' in V else np.nan
            return t, u, la, lo, pid
    with ThreadPoolExecutor(threads) as ex:
        paths = list(ex.map(one, names))
    rows = [read(p) for p in paths]
    df = pd.DataFrame(rows, columns=['t', 'u', 'lat', 'lon', 'dive'])
    df['time'] = pd.concat([_decode_time(g['t'].to_numpy(), u).to_series(index=g.index) for u, g in df.groupby('u')]).sort_index()
    attrs = {}
    with netCDF4.Dataset(os.path.join(d, names[0])) as ds:
        attrs = {n: {a: str(getattr(v, a))[:200] for a in VAR_ATTRS if hasattr(v, a)} for n, v in ds.variables.items()}
    return df[['time', 'lat', 'lon', 'dive']], attrs, len(names)


def erddap_var_attrs(ref):
    """Variable attributes of one ERDDAP dataset (standard_name, long_name, units)."""
    base, did = ref['url'].rsplit('/tabledap/', 1)
    out = {}
    for l in csv.reader(get(f'{base}/info/{did}/index.csv').splitlines()):
        if len(l) >= 5 and l[0] == 'attribute' and l[1] != 'NC_GLOBAL' and l[2] in VAR_ATTRS:
            out.setdefault(l[1], {})[l[2]] = l[4][:200]
        elif len(l) >= 2 and l[0] == 'variable':
            out.setdefault(l[1], {})
    return out


# ---------------- files ----------------
# (time, lat, lon) triples on one dimension, in order of preference; the GPS fixes are the fallback
# for files whose main time axis is empty (e.g. some Coriolis EGO files hold only TIME_GPS fixes)
TRIPLES = (('JULD', 'LATITUDE', 'LONGITUDE'), ('TIME', 'LATITUDE', 'LONGITUDE'),
           ('time', 'latitude', 'longitude'), ('TIME_GPS', 'LATITUDE_GPS', 'LONGITUDE_GPS'))


def pick_triple(shape, has_units):
    """First triple whose three variables are 1-D (> 1 value) with time units. Lengths may differ a little
    (netCDF4/HDF5 stores unwritten trailing records of an unlimited dimension as missing); readers truncate
    to the shortest."""
    for tv, lav, lov in TRIPLES:
        sh = [shape(v) for v in (tv, lav, lov)]
        if all(x is not None and len(x) == 1 and x[0] > 1 for x in sh) and has_units(tv):
            return tv, lav, lov
    raise ValueError('no 1-D time/lat/lon')


def _decode_time(vals, units):
    import cftime
    vals = np.asarray(vals, dtype=float)
    out = np.full(vals.shape, np.datetime64('NaT'), dtype='datetime64[s]')
    ok = np.isfinite(vals) & (np.abs(vals) < 1e12)
    if ok.any():
        dt = cftime.num2date(vals[ok], units, only_use_cftime_datetimes=False, only_use_python_datetimes=True)
        out[ok] = np.array([np.datetime64(x.replace(tzinfo=None), 's') for x in dt])
    return pd.to_datetime(out).tz_localize('UTC')


def _masked(v, fill):
    a = np.asarray(v, dtype=float)
    if fill is not None:
        a[a == float(np.asarray(fill).ravel()[0])] = np.nan
    a[np.abs(a) > 1e10] = np.nan
    return a


def read_h5(fobj):
    import h5py
    from common import _s
    with h5py.File(fobj, 'r') as h:
        attrs = {n: {a: _s(o.attrs[a])[:200] for a in VAR_ATTRS if a in o.attrs} for n, o in h.items() if isinstance(o, h5py.Dataset)}
        tv, lav, lov = pick_triple(lambda n: h[n].shape if n in h and isinstance(h[n], h5py.Dataset) else None,
                                   lambda n: 'units' in h[n].attrs)
        n = min(h[v].shape[0] for v in (tv, lav, lov))
        t = _decode_time(_masked(h[tv][:n], h[tv].attrs.get('_FillValue')), _s(h[tv].attrs['units']))
        la = _masked(h[lav][:n], h[lav].attrs.get('_FillValue'))
        lo = _masked(h[lov][:n], h[lov].attrs.get('_FillValue'))
    return pd.DataFrame({'time': t, 'lat': la, 'lon': lo}), attrs


def read_nc(path):
    import netCDF4
    ds = netCDF4.Dataset(path)
    try:
        ds.set_auto_mask(False)
        attrs = {n: {a: str(getattr(v, a))[:200] for a in VAR_ATTRS if hasattr(v, a)} for n, v in ds.variables.items()}
        V = ds.variables
        tv, lav, lov = pick_triple(lambda n: V[n].shape if n in V else None, lambda n: hasattr(V[n], 'units'))
        n = min(V[v].shape[0] for v in (tv, lav, lov))
        t = _decode_time(_masked(V[tv][:n], getattr(V[tv], '_FillValue', None)), V[tv].units)
        la = _masked(V[lav][:n], getattr(V[lav], '_FillValue', None))
        lo = _masked(V[lov][:n], getattr(V[lov], '_FillValue', None))
    finally:
        ds.close()
    return pd.DataFrame({'time': t, 'lat': la, 'lon': lo}), attrs


def download(url, cache):
    """Whole-file download into the local file cache (kept for later analyses); reuse if present."""
    path = os.path.join(cache, url.rstrip('/').rsplit('/', 1)[-1])
    if os.path.exists(path):
        return path
    os.makedirs(cache, exist_ok=True)
    with urllib.request.urlopen(url, timeout=600) as r, open(path + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(path + '.part', path)
    return path


def imos_current(url):
    """IMOS renames a real-time file each time it updates (END-<date> in the name): find the current one."""
    from common import IMOS_S3
    folder = url[len(IMOS_S3):].rsplit('/', 1)[0] + '/'
    keys = re.findall(r'<Key>([^<]+\.nc)</Key>', get(f'{IMOS_S3}?list-type=2&prefix={folder}', timeout=120, tries=3))
    if not keys:
        raise ValueError('no .nc file in the IMOS folder now')
    return IMOS_S3 + sorted(keys)[-1]


def coriolis_profiles_track(dep_path, cache, threads=2):
    """Coriolis deployments held only as Argo-style per-profile files (<dep>/profiles/R<wmo>_<date>_<n>.nc):
    download them (kept in cache/<dep>/profiles/) and take each profile's JULD, LATITUDE, LONGITUDE."""
    import netCDF4
    from concurrent.futures import ThreadPoolExecutor
    from common import CORIOLIS
    base = f'{CORIOLIS}{dep_path}/profiles/'
    names = sorted(set(re.findall(r'href="([^"?/]+\.nc)"', get(base, timeout=300, tries=3))))
    if not names:
        raise ValueError('no trajectory file and no profile files')
    d = os.path.join(cache, dep_path, 'profiles')
    os.makedirs(d, exist_ok=True)
    def one(n):
        p = os.path.join(d, n)
        for k in range(6):  # retry: DNS lookups and connections fail now and then under sustained load
            if os.path.exists(p):
                break
            try:
                with urllib.request.urlopen(base + n, timeout=300) as r, open(p + '.part', 'wb') as f:
                    f.write(r.read())
                os.replace(p + '.part', p)
            except (OSError, urllib.error.URLError):
                if k == 5:
                    raise
                time.sleep(5 * 2 ** k)
        return p
    def read(p):  # netCDF4 is not thread-safe: read one file at a time
        with netCDF4.Dataset(p) as ds:
            ds.set_auto_mask(False)
            V = ds.variables
            t = _decode_time(_masked(V['JULD'][:1], getattr(V['JULD'], '_FillValue', None)), V['JULD'].units)[0]
            return t, float(_masked(V['LATITUDE'][:1], getattr(V['LATITUDE'], '_FillValue', None))[0]), \
                float(_masked(V['LONGITUDE'][:1], getattr(V['LONGITUDE'], '_FillValue', None))[0]), os.path.basename(p)
    with ThreadPoolExecutor(threads) as ex:
        paths = list(ex.map(one, names))
    rows = [read(p) for p in paths]
    df = pd.DataFrame(rows, columns=['time', 'lat', 'lon', 'file'])
    df['dive'] = np.arange(len(df), dtype=float)
    return df[['time', 'lat', 'lon', 'dive']], len(names)


def file_track(ref, cache):
    try:
        path = download(ref['url'], cache)
    except urllib.error.HTTPError as e:
        from common import IMOS_S3
        if e.code not in (403, 404) or not ref['url'].startswith(IMOS_S3):
            raise
        path = download(imos_current(ref['url']), cache)
    with open(path, 'rb') as f:
        hdf5 = f.read(4) == b'\x89HDF'
    return read_h5(path) if hdf5 else read_nc(path)

# ---------------- one deployment ----------------
def order_refs(source, refs):
    """Recovered before real time; ERDDAP datasets with a dive variable first; files with a URL only."""
    refs = [r for r in refs if r.get('url')]
    if source == 'Coriolis':  # P files are full resolution; R files are lighter and cover the same track
        rank = lambda r: (0 if r['stream'] == 'Recovered' else 1, 0 if r['url'].endswith('_R.nc') else 1)
    else:
        rank = lambda r: (0 if r['stream'] == 'Recovered' else 1, 0 if pick(r['variables'], *DIVE_VARS) else 1)
    return sorted(refs, key=rank)


def fetch_one(job):
    row, refs, store = job
    source, dep = row['source'], row['deployment']
    dep_id = f'{source}:{dep}'
    start = pd.Timestamp(row['start_date'], tz='UTC') - pd.Timedelta(days=5) if row['start_date'] else None
    end = pd.Timestamp(row['end_date'], tz='UTC') + pd.Timedelta(days=2) if row['end_date'] else None
    log = {'dep_id': dep_id, 'source': source, 'deployment': dep, 'end_date': row['end_date'],
           'fetched_at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}
    var_attrs, errs = {}, []
    if source == 'Coriolis' and refs and not any(r.get('url') for r in refs):  # no trajectory file: try per-profile files
        try:
            df, nf = coriolis_profiles_track(dep, os.path.join(store, 'files', 'source=Coriolis'))
            n_raw = len(df)
            df = clean(df, start, end)
            if not df.empty:
                p = track_path(store, source, dep)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                df.assign(decimation='dive').to_parquet(p, index=False)
                log.update(status='ok', ref=f'{dep}/profiles', stream='NRT', decimation='dive', n_points=len(df), n_dropped=n_raw - len(df),
                           t_first=str(df.time.iloc[0])[:19], t_last=str(df.time.iloc[-1])[:19], message=f'{nf} per-profile files (no trajectory file)')
                return log, []
            errs.append(f'{dep}/profiles: empty ({n_raw} before cleaning)')
        except Exception as e:
            errs.append(f'{dep}/profiles: {type(e).__name__}: {str(e)[:120]}')
    for ref in order_refs(source, refs):
        try:
            if source == 'OTN' and '/tabledap/' in ref['url']:  # whole query, then week by week, then per-profile files
                try:
                    df, dec = erddap_track(ref, tries=1, timeout=150)
                except Exception as e:
                    s0 = pd.Timestamp(ref.get('start') or row['start_date'], tz='UTC') if (ref.get('start') or row['start_date']) else None
                    s1 = pd.Timestamp((ref.get('end') or row['end_date'] or '')[:19] or None, tz='UTC') if (ref.get('end') or row['end_date']) else None
                    try:
                        if s0 is None or s1 is None:
                            raise ValueError('no time range for weekly queries')
                        df, dec = erddap_track_weekly(ref, s0, s1)
                        errs.append(f"{ref['id']}: whole-dataset query {type(e).__name__}; used weekly queries")
                    except Exception as e2:
                        errs.append(f"{ref['id']}: ERDDAP {type(e2).__name__}: {str(e2)[:80]}; used /files/")
                        df, attrs, nf = otn_files_track(ref, os.path.join(store, 'files', 'source=OTN'))
                        var_attrs[ref['id']] = attrs
                        dec = 'dive'
                        errs.append(f'{nf} per-profile files')
            elif source in ERDDAP_SOURCES and '/tabledap/' in ref['url']:
                df, dec = erddap_track(ref)
            else:
                raw, attrs = file_track(ref, os.path.join(store, 'files', f'source={source}'))
                var_attrs[ref['id']] = attrs
                df, dec = hourly(clean(raw, start, end)), 'hour'
            n_raw = len(df)
            df = clean(df, start, end)
            if df.empty:
                errs.append(f"{ref['id']}: empty ({n_raw} before cleaning)")
                continue
            p = track_path(store, source, dep)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            df['dive'] = df['dive'].astype('float64')
            df.assign(decimation=dec).to_parquet(p, index=False)
            log.update(status='ok', ref=ref['id'], stream=ref['stream'], decimation=dec, n_points=len(df), n_dropped=n_raw - len(df),
                       t_first=str(df.time.iloc[0])[:19], t_last=str(df.time.iloc[-1])[:19], message='; '.join(errs))
            break
        except Exception as e:
            errs.append(f"{ref['id']}: {type(e).__name__}: {str(e)[:150]}")
    else:
        log.update(status='empty' if errs and all('empty' in x for x in errs) else 'error', message='; '.join(errs) or 'no data reference')
    # variable attributes of every ERDDAP dataset in this deployment (files were read above)
    for ref in refs:
        if source in ERDDAP_SOURCES and '/tabledap/' in ref.get('url', '') and ref['id'] not in var_attrs:
            try:
                var_attrs[ref['id']] = erddap_var_attrs(ref)
            except Exception:
                pass
    return log, [{'dep_id': dep_id, 'ref': k, 'variables': v} for k, v in var_attrs.items()]


def last_log(store):
    p = os.path.join(store, 'fetch_log.jsonl')
    out = {}
    if os.path.exists(p):
        for l in open(p):
            r = json.loads(l)
            out[r['dep_id']] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True)
    ap.add_argument('--store', required=True)
    ap.add_argument('--since', default=(datetime.date.today() - datetime.timedelta(days=365)).isoformat())
    ap.add_argument('--sources', nargs='*')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--procs', type=int, default=4)
    a = ap.parse_args()
    os.makedirs(a.store, exist_ok=True)
    rows = list(csv.DictReader(open(os.path.join(a.work, 'glider_deployments.csv'))))
    refs = {}
    for l in open(os.path.join(a.work, 'deployment_refs.jsonl')):
        r = json.loads(l)
        refs[r['dep_id']] = r['refs']
    done = last_log(a.store)
    todo = []
    for r in rows:
        dep_id = f"{r['source']}:{r['deployment']}"
        d = r['end_date'] or r['start_date']  # undated deployments are included only when fetching everything
        if (d < a.since if d else a.since > '1900-01-01') or (a.sources and r['source'] not in a.sources):
            continue
        prev = done.get(dep_id)
        if a.force or not prev or prev['status'] == 'error' or prev.get('end_date') != r['end_date']:
            todo.append((r, refs.get(dep_id, []), a.store))
    print(f'since {a.since}: {len(todo)} deployments to fetch', flush=True)
    ok = 0
    with open(os.path.join(a.store, 'fetch_log.jsonl'), 'a') as flog, open(os.path.join(a.store, 'var_attrs.jsonl'), 'a') as fva, \
            cf.ProcessPoolExecutor(a.procs) as ex:
        for i, (log, va) in enumerate(ex.map(fetch_one, todo), 1):
            flog.write(json.dumps(log) + '\n')
            flog.flush()
            for v in va:
                fva.write(json.dumps(v) + '\n')
            ok += log['status'] == 'ok'
            if i % 20 == 0 or log['status'] != 'ok':
                print(f"{i}/{len(todo)} {log['dep_id']} {log['status']} {log.get('n_points', '')} {log.get('message', '')[:160]}", flush=True)
    print(f'DONE ok {ok} of {len(todo)}', flush=True)


if __name__ == '__main__':
    main()
