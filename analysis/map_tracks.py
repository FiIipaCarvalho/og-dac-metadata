"""Static map of glider tracks in a window, coloured by DAC.

--by tagged: OceanGliders-tagged deployments in full DAC colour, untagged pale.
--by og1:    deployments available in OG1.0 format in full DAC colour, others pale.

    python3.11 map_tracks.py --since 2025-10-28 [--until YYYY-MM-DD] [--by tagged|og1] [--out tracks.png]
"""
import argparse
from datetime import date, timedelta

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import matplotlib.colors as mc
from matplotlib.lines import Line2D

from store import DAC_COLORS, load_tracks

PC = ccrs.PlateCarree()
REGIONS = {'Europe': (-15, 25, 40, 66), 'North America': (-130, -55, 15, 52),
           'Australia': (105, 160, -48, -8)}


def pale(c, f=0.6):
    """Blend a colour towards white."""
    return tuple(1 - (1 - v) * (1 - f) for v in mc.to_rgb(c))


def segments(lon, lat):
    """Split a track where it crosses the dateline or has a large position gap."""
    br = np.where((np.abs(np.diff(lon)) > 180) | (np.hypot(np.diff(lon), np.diff(lat)) > 5))[0] + 1
    return zip(np.split(lon, br), np.split(lat, br))


BY = {'tagged': ('tagged', 'OceanGliders tagged', 'not tagged'),
      'og1': ('og1', 'OG1.0 format', 'not OG1.0')}


def draw(ax, tr, key, extent=None):
    if extent:
        ax.set_extent(extent, PC)
    else:
        ax.set_global()
    ax.add_feature(cfeature.LAND, facecolor='#e6e6e6', zorder=0)
    ax.coastlines(lw=0.3, color='#888')
    for on in (False, True):
        for dep, g in tr[tr[key] == on].groupby('dep_id', sort=False):
            g = g.sort_values('time')
            c = DAC_COLORS[g.source.iat[0]]
            for x, y in segments(g.lon.values, g.lat.values):
                ax.plot(x, y, color=c if on else pale(c), transform=PC, lw=1.6 if on else 0.8,
                        zorder=3 if on else 2, solid_capstyle='round')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default=str(date.today() - timedelta(days=365)))
    ap.add_argument('--until')
    ap.add_argument('--by', choices=BY, default='tagged')
    ap.add_argument('--out', default='tracks.png')
    a = ap.parse_args()
    tr = load_tracks(a.since, a.until)
    deps = tr.drop_duplicates('dep_id')
    key, on_label, off_label = BY[a.by]

    fig = plt.figure(figsize=(14, 11))
    draw(fig.add_axes([0.02, 0.40, 0.96, 0.55], projection=PC), tr, key)
    for i, (name, ext) in enumerate(REGIONS.items()):
        ax = fig.add_axes([0.02 + i * 0.33, 0.06, 0.30, 0.32], projection=PC)
        draw(ax, tr, key, ext)
        ax.set_title(name, fontsize=10)

    n = deps.groupby('source').agg(n=('dep_id', 'size'), tag=('tagged', 'sum'), og1=('og1', 'sum'))
    h = [Line2D([], [], color=DAC_COLORS[s], lw=2.5,
                label=f'{s}: {r.n} ({r.tag} tagged, {r.og1} OG1)')
         for s in DAC_COLORS if s in n.index for r in [n.loc[s]]]
    h += [Line2D([], [], color='#555', lw=1.6, label=f'{on_label} (full colour)'),
          Line2D([], [], color=pale('#555'), lw=0.8, label=f'{off_label} (pale colour)')]
    fig.legend(handles=h, loc='upper center', ncol=4, fontsize=8.5, frameon=False,
               bbox_to_anchor=(0.5, 0.995))
    fig.text(0.5, 0.02, f'Glider deployments active {a.since} to {a.until or date.today()}: '
             f'{len(deps)} with tracks, {int(deps.tagged.sum())} tagged, {int(deps.og1.sum())} OG1. '
             'One point per dive (or per hour where no dive/profile number).',
             ha='center', fontsize=9)
    fig.savefig(a.out, dpi=150)
    print(a.out, len(deps), 'deployments,', len(tr), 'points')


if __name__ == '__main__':
    main()
