"""Static maps of glider tracks, coloured by DAC, for several time windows.

For each window, two maps:
  by tagged: OceanGliders-tagged deployments in full DAC colour, untagged pale.
  by og1:    deployments available in OG1.0 format in full DAC colour, others pale.

    python3.11 map_tracks.py [--until 2026-10-28] [--windows all 2016 2021 2y 12m years] [--outdir ../data]

writes <outdir>/tracks_<window>_<by>.png; 'years' adds one map per calendar year
(deployments active at any time in that year), tracks_y<YYYY>_<by>.png.
"""
import argparse
from datetime import date, timedelta

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib
matplotlib.use('Agg')
import matplotlib.colors as mc
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.lines import Line2D

from store import DAC_COLORS, load_deployments, load_tracks

PC = ccrs.PlateCarree()
# BOON published regions first (Northeast Pacific, Northwest Atlantic, Baltic Sea), then other areas
REGIONS = {'BOON Northeast Pacific': (-160, -115, 28, 62), 'BOON Northwest Atlantic': (-82, -45, 24, 62),
           'BOON Baltic Sea': (9, 31, 53, 66.5), 'Europe and Mediterranean': (-35, 40, 28, 72),
           'Australia': (105, 165, -50, -5)}
BY = {'tagged': ('tagged', 'OceanGliders tagged', 'not tagged'),
      'og1': ('og1', 'OG1.0 format', 'not OG1.0')}


def windows(until):
    u = date.fromisoformat(until)
    return {'all': (None, 'all deployments'), '2016': ('2016-01-01', 'since 2016'),
            '2021': ('2021-01-01', 'since 2021'),
            '2y': (str(u - timedelta(days=730)), 'last 2 years'),
            '12m': (str(u - timedelta(days=365)), 'last 12 months')}


def pale(c, f=0.4):
    """Blend a colour towards white."""
    return tuple(1 - (1 - v) * (1 - f) for v in mc.to_rgb(c))


def segments(lon, lat):
    """Split a track where it crosses the dateline or has a large position gap."""
    br = np.where((np.abs(np.diff(lon)) > 180) | (np.hypot(np.diff(lon), np.diff(lat)) > 5))[0] + 1
    return [np.column_stack(s) for s in zip(np.split(lon, br), np.split(lat, br)) if len(s[0]) > 1]


def layers(tr, key):
    """Line segments grouped into (colour, width, zorder) layers; highlighted tracks on top."""
    out = []
    for on in (False, True):
        for src, g in tr[tr[key] == on].groupby('source'):
            segs = [s for _, d in g.groupby('dep_id', sort=False) for s in segments(d.lon.values, d.lat.values)]
            c = DAC_COLORS[src]
            out.append((segs, c if on else pale(c), 1.6 if on else 1.0, 3 if on else 2))
    return out


def draw(ax, lay, extent=None):
    if extent:
        ax.set_extent(extent, PC)
    else:
        ax.set_global()
    ax.add_feature(cfeature.LAND, facecolor='#e6e6e6', zorder=0)
    ax.coastlines(lw=0.3, color='#888')
    for segs, c, lw, z in lay:
        ax.add_collection(LineCollection(segs, colors=[c], linewidths=lw, zorder=z,
                                         transform=PC, capstyle='round'))


def render(tr, by, label, n_missing, out):
    key, on_label, off_label = BY[by]
    deps = tr.drop_duplicates('dep_id')
    lay = layers(tr, key)
    fig = plt.figure(figsize=(14, 15))
    draw(fig.add_axes([0.02, 0.50, 0.96, 0.44], projection=PC), lay)
    for i, (name, ext) in enumerate(REGIONS.items()):
        x = 0.02 + (i % 3) * 0.33 if i < 3 else 0.185 + (i - 3) * 0.33  # 3 on top, 2 centred below
        ax = fig.add_axes([x, 0.27 - (i // 3) * 0.235, 0.30, 0.20], projection=PC)
        ax.set_anchor('N')
        draw(ax, lay, ext)
        ax.set_title(name, fontsize=10)

    n = deps.groupby('source').agg(n=('dep_id', 'size'), tag=('tagged', 'sum'), og1=('og1', 'sum'))
    h = [Line2D([], [], color=DAC_COLORS[s], lw=2.5,
                label=f'{s}: {r.n} ({r.tag} tagged, {r.og1} OG1)')
         for s in DAC_COLORS if s in n.index for r in [n.loc[s]]]
    h += [Line2D([], [], color='#555', lw=1.6, label=f'{on_label} (full colour)'),
          Line2D([], [], color=pale('#555'), lw=1.0, label=f'{off_label} (pale colour)')]
    fig.legend(handles=h, loc='upper center', ncol=4, fontsize=8.5, frameon=False,
               bbox_to_anchor=(0.5, 0.995))
    miss = f' {n_missing} more in the catalogue {"has" if n_missing == 1 else "have"} no track yet.' if n_missing else ''
    fig.text(0.5, 0.012, f'Glider deployments, {label}: {len(deps)} with tracks, '
             f'{int(deps.tagged.sum())} tagged, {int(deps.og1.sum())} OG1.{miss} '
             'One point per dive (or per hour where no dive/profile number).',
             ha='center', fontsize=9)
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print(out, len(deps), 'deployments')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--until', default=str(date.today()))
    ap.add_argument('--windows', nargs='+', default=['all', '2016', '2021', '2y', '12m', 'years'])
    ap.add_argument('--by', nargs='+', default=list(BY), choices=BY)
    ap.add_argument('--outdir', default='.')
    a = ap.parse_args()
    d = load_deployments()
    act = d['end_date'].fillna(d['start_date'])
    win = windows(a.until)
    years = range(int(d.loc[d['start_year'] >= 1990, 'start_year'].min()), int(a.until[:4]) + 1)
    for y in years:
        win[f'y{y}'] = (f'{y}-01-01', f'{y}', f'{y}-12-31' if y < int(a.until[:4]) else a.until)
    keys = [k for w in a.windows for k in ([f'y{y}' for y in years] if w == 'years' else [w])]
    for w in keys:
        since, label, until = (win[w] + (a.until,))[:3]
        tr = load_tracks(since, until)
        if tr.empty:
            tr = pd.DataFrame(columns=['dep_id', 'source', 'time', 'lat', 'lon', 'tagged', 'og1'])
        cand = d[((act >= since) if since else act.notna()) & (d['start_date'] <= until)]
        n_missing = int((cand['track_status'] != 'ok').sum())
        label = f'{label} ({since} to {until})' if since else f'{label} (to {until})'
        for by in a.by:
            render(tr, by, label, n_missing, f'{a.outdir}/tracks_{w}_{by}.png')

if __name__ == '__main__':
    main()
