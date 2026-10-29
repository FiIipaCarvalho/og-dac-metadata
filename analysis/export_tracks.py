"""Export all tracks as one compact JSON for the zoomable (Leaflet) map on the dashboard.

Tracks are thinned to one point every --km kilometres (plus the last point), stored as
integer hundredths of a degree (about 1 km), and split where they cross the dateline or jump
more than 5 degrees. Each segment is delta-encoded: [lat0, lon0, dlat1, dlon1, ...]; the page
adds the differences back up and divides by 100.

    python3.11 export_tracks.py [--km 5] [--until 2026-10-28] [--out ../data/tracks.json]

Output: {"until", "sources": [...], "fields": [...], "deps": [[...], ...]} with one row per deployment:
dep (deployment name), src (index into sources), inst, t0, t1 (first/last track date),
tag, og1 (0/1), fmt, segs (list of delta-encoded integer arrays, see above).
"catalogue": [src, start, end] for every catalogue deployment (end = start when missing),
so the page can show how many deployments in a window are not mapped.
"""
import argparse
import json
from datetime import date

import numpy as np

from store import DAC_COLORS, load_tracks, load_deployments

FIELDS = ['dep', 'src', 'inst', 't0', 't1', 'tag', 'og1', 'fmt', 'segs']


def thin(lat, lon, km):
    """Keep a point when it is at least km from the last kept one (equirectangular distance)."""
    keep, la0, lo0 = [0], lat[0], lon[0]
    for i in range(1, len(lat)):
        dx = (lon[i] - lo0) * np.cos(np.radians(lat[i])) * 111.2
        dy = (lat[i] - la0) * 111.2
        if dx * dx + dy * dy >= km * km:
            keep.append(i)
            la0, lo0 = lat[i], lon[i]
    if keep[-1] != len(lat) - 1:
        keep.append(len(lat) - 1)
    return np.array(keep)


def segs(lat, lon):
    br = np.where((np.abs(np.diff(lon)) > 180) | (np.hypot(np.diff(lon), np.diff(lat)) > 5))[0] + 1
    out = []
    for a, o in zip(np.split(lat, br), np.split(lon, br)):
        q = np.column_stack([np.round(a * 100), np.round(o * 100)]).astype(int)
        q[1:] = np.diff(q, axis=0)
        out.append(q.ravel().tolist())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--km', type=float, default=5)
    ap.add_argument('--until', default=str(date.today()), help='snapshot date the windows count back from')
    ap.add_argument('--out', default='tracks.json')
    a = ap.parse_args()
    meta = load_deployments().set_index('dep_id')
    tr = load_tracks()
    src = list(DAC_COLORS)
    rows, npts = [], 0
    for dep_id, g in tr.sort_values('time').groupby('dep_id', sort=False):
        m = meta.loc[dep_id]
        lat, lon = g['lat'].to_numpy(), g['lon'].to_numpy()
        k = thin(lat, lon, a.km)
        npts += len(k)
        inst = m['institution'] if isinstance(m['institution'], str) else ''
        rows.append([m['deployment'], src.index(m['source']), inst,
                     str(g['time'].iat[0])[:10], str(g['time'].iat[-1])[:10],
                     int(m['tagged']), int(m['og1_available']), m['format'], segs(lat[k], lon[k])])
    cat = meta[meta['start_date'].notna() & meta['source'].isin(src)]
    catalogue = [[src.index(r.source), r.start_date[:10], (r.end_date if isinstance(r.end_date, str) else r.start_date)[:10]]
                 for r in cat.itertuples()]
    out = {'until': a.until, 'catalogue': catalogue, 'sources': src, 'colors': DAC_COLORS, 'fields': FIELDS, 'deps': rows}
    with open(a.out, 'w') as f:
        json.dump(out, f, separators=(',', ':'))
    print(a.out, len(rows), 'deployments,', npts, 'points')


if __name__ == '__main__':
    main()
