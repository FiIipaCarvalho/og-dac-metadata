"""Merge the harvests into one row per glider deployment per data centre: DIR/glider_deployments.csv.

    python3.11 build.py --work DIR [--recent 2024-10-27]

Columns: tagging (network, site), WMO ID, format, best data mode, glider-days, QC flags (present,
CF-described, scheme), platform, institution (normalised), and a cross-centre match group
(same WMO ID and start date within 3 days). Wave gliders (surface vehicles) are left out.
"""
import argparse
import collections
import csv
import datetime
import json
import os
import re

from common import CORIOLIS, ERDDAPS, IMOS_S3

# ---------------- institution names ----------------
WHOLE = [  # checked on the whole institution string first
    ('c-proof', 'C-PROOF'), ('ooi coastal & global', 'OOI Coastal & Global Scale Nodes'),
    ('ooi coastal endurance', 'OOI Coastal Endurance'), ('great lakes observing system', 'Great Lakes Observing System / NOAA GLERL'),
    ('us epa', 'US EPA Great Lakes'), ('scripps', 'Scripps Institution of Oceanography'),
    ('woods hole', 'Woods Hole Oceanographic Institution'), ('wood hole', 'Woods Hole Oceanographic Institution'),
    ('ensenada', 'CICESE (with GCOOS)'),
]
PART = [  # applied to the first-listed organisation
    ('naval oceanographic office', 'Naval Oceanographic Office'), ('navoceano', 'Naval Oceanographic Office'),
    ('bermuda institute', 'Bermuda Institute of Ocean Sciences'), ('oregonstate', 'Oregon State University'),
    ('oregon state', 'Oregon State University'), ('minnesota', 'University of Minnesota Duluth'),
    ('massachus', 'University of Massachusetts Dartmouth'), ('virginia institute', 'Virginia Institute of Marine Science'),
    ('mote marine', 'Mote Marine Laboratory'), ('texas a&m', 'Texas A&M University'), ('tamu', 'Texas A&M University'),
    ('south florida', 'University of South Florida'), ('nova/usf', 'University of South Florida'), ('usf', 'University of South Florida'),
    ('teledyne', 'Teledyne Webb Research'), ('twrc', 'Teledyne Webb Research'), ('hereon', 'Helmholtz-Zentrum Hereon'),
    ('helmhotz', 'Helmholtz-Zentrum Hereon'), ('skidaway', 'Skidaway Institute of Oceanography'), ('rutgers', 'Rutgers University'),
    ('mbari', 'Monterey Bay Aquarium Research Institute'), ('monterey bay', 'Monterey Bay Aquarium Research Institute'),
    ('stony brook', 'Stony Brook University'), ('delaware', 'University of Delaware'), ('hawai', "University of Hawai'i at Manoa"),
    ('pacific marine environmental', 'NOAA PMEL'), ('southwest fisheries', 'NOAA SWFSC'), ('swfsc', 'NOAA SWFSC'),
    ('graduate school of oceanography', 'URI Graduate School of Oceanography'),
    ('atlantic oceanographic and meteorological', 'NOAA AOML'), ('alaska fairbanks', 'University of Alaska Fairbanks'),
    ('applied physics laboratory', 'University of Washington APL'), ('university of washington', 'University of Washington APL'),
    ('southern mississippi', 'University of Southern Mississippi'),
]
ALIAS = {
    'noc': 'National Oceanography Centre', 'nocs': 'National Oceanography Centre', 'nocl': 'National Oceanography Centre',
    'nocs nocl': 'National Oceanography Centre', 'dfo': 'Fisheries and Oceans Canada', 'bodc': 'National Oceanography Centre', 'national oceanography centre': 'National Oceanography Centre',
    'voiceoftheocean': 'Voice of the Ocean Foundation', 'voice of the ocean foundation': 'Voice of the Ocean Foundation',
    'anfog (australian national facility for ocean gliders)': 'ANFOG (IMOS)', 'otn': 'Ocean Tracking Network',
    'dalhousie, oceanography': 'Dalhousie University', 'uea': 'University of East Anglia', 'hzg': 'Helmholtz-Zentrum Hereon',
    'ogs/maos': 'OGS', 'socib data center': 'SOCIB', 'el instituto del mar del perú (imarpe)': 'IMARPE',
    'nato sto-cmre (science and technology organization - centre for maritime research and experimentation)': 'NATO STO-CMRE',
}


def canon(raw, source):
    s = (raw or '').strip()
    low = s.lower()
    if source == 'IOOS':
        for k, v in WHOLE:
            if k in low:
                return v
        parts = [p.strip() for p in re.split(r'[,;]', s) if p.strip()]
        if len(parts) >= 3:
            return 'Multi-institution partnership'
        first = parts[0] if parts else 'Unknown'
        for k, v in PART:
            if k in first.lower():
                return v
        return first or 'Unknown'
    if low.startswith('socib'):
        return 'SOCIB'
    return ALIAS.get(low, s) or 'Unknown'


# ---------------- field rules ----------------
def ymd(s):
    s = (s or '').strip()
    if re.match(r'^\d{8}$', s):
        return f'{s[:4]}-{s[4:6]}-{s[6:]}'
    return s[:10] if re.match(r'^\d{4}-\d{2}-\d{2}', s) else ''


def good_wmo(v):
    v = str(v or '').strip()
    return v if re.match(r'^\d{5,8}$', v) and set(v) != {'0'} else ''


def tag(v):
    v = (v or '').strip()
    return v if v.startswith('OceanGliders') else ''


def fmt_of(g):
    conv, fv, dt = g.get('Conventions', ''), g.get('format_version', ''), g.get('data_type', '')
    if re.search(r'OG-?1', conv) or re.search(r'OG-?1', fv):
        return 'OG1.0'
    if 'EGO' in dt or 'EGO' in conv or g.get('naming_authority') == 'EGO':
        return 'EGO'
    if 'IOOS_Glider_NetCDF' in fv:
        return 'IOOS DAC'
    if 'IMOS' in conv:
        return 'IMOS'
    if 'IOOSNGDAC' in fv or 'Unidata Dataset Discovery' in conv:  # older IOOS Glider DAC files
        return 'IOOS DAC'
    return 'Other'


FMT_RANK = ['OG1.0', 'EGO', 'IOOS DAC', 'IMOS', 'Other']
MODE_RANK = {'D': 4, 'P': 3, 'M': 2, 'A': 1, 'R': 0}  # A = real time, adjusted (EGO/Argo)


def qc_of(qc, names):
    """(present, cf, scheme) for one file."""
    flagged = {k: v for k, v in (qc or {}).items() if v.get('flag_values') or v.get('flag_meanings')}
    named = any(n.lower().endswith('_qc') for n in names or [])
    if not flagged:
        return ('1' if named else '0'), '0', ('Not described' if named else 'None')
    def scheme_of(values):
        if values & {'A', 'B', 'Q'}:
            return 'SeaDataNet L20'
        if values == {'1', '2', '3', '4', '9'}:
            return 'IOC/QARTOD 1-4,9'
        if values >= {'0', '1', '2', '3', '4', '9'}:
            return 'Argo-style 0-9'
        return 'Other'
    cf, schemes = True, collections.Counter()
    for v in flagged.values():
        vals = [x for x in re.split(r'[,\s]+', v.get('flag_values', '').strip('[]')) if x]
        means = v.get('flag_meanings', '').split()
        if not vals or not means or len(vals) != len(means):
            cf = False
        if vals:  # each flag variable votes; metadata flags (e.g. position) shouldn't decide the file's scheme alone
            schemes[scheme_of({re.sub(r'\.0+$', '', x.strip("b'\"")) for x in vals})] += 1
    scheme = schemes.most_common(1)[0][0] if schemes else 'Other'
    return '1', '1' if cf else '0', scheme


# ---------------- stream (real time vs recovered) and QC level ----------------
LEVEL_RANK = {'Delayed mode': 4, 'Automatic QC': 3, 'No QC': 2, 'Not stated': 1, 'Not read': 0}
NO_QC = ('no qc', 'no quality control', 'no qa/qc', 'no implied assurance', 'as is with no', 'level 0',
         'no corrections or data screening', 'engineering units', 'timestamp and gps positions checked')
AUTO_QC = ('automatic', 'qartod', 'quality control flags', 'quality control variables provided', 'level 2', 'argo qc',
           "qc'd", 'qa/qc measures have been run', 'quality assurance and quality control performed', 'quality control provided by',
           'ego gliders data processing chain', '10.13155/51485')


def stream_of(src, f):
    did, mode = f.get('dataset_id', ''), (f.get('mode') or '').upper()
    if src == 'IOOS':
        return 'Recovered' if did.endswith('-delayed') else 'NRT'
    if src == 'VOTO':
        return 'Recovered' if did.startswith(('delayed_', 'OG_complete_')) else 'NRT'
    if src == 'BODC':
        return 'Recovered' if 'Recovery' in did or mode in ('P', 'D') or re.search(r'_[DP]$', did) else 'NRT'
    if src == 'OTN':
        return 'Recovered' if did.endswith('_delayed') else 'NRT'
    if src == 'Coriolis':  # P files are full resolution (checked: 1.6-2 s vs 4-14 s steps in the R files)
        return 'Recovered' if mode in ('P', 'D') else 'NRT'
    if src == 'IMOS':
        return 'NRT' if 'REALTIME' in did else 'Recovered'
    return 'NRT'


def qc_level_of(f):
    if f.get('unread'):
        return 'Not read'
    g = f.get('globals', {})
    pl = (g.get('processing_level') or '').lower()
    dm = (g.get('data_mode') or '').strip().lower()
    extra = ' '.join(str(g.get(k, '')) for k in ('qc_manual', 'data_processing_chain_name')).lower()
    if 'manually' in pl or dm in ('d', 'delayed mode'):
        return 'Delayed mode'
    if any(k in pl for k in NO_QC) or dm == 'near real time mode':  # IMOS FV00 real-time files are raw
        return 'No QC'
    if any(k in pl + ' ' + extra for k in AUTO_QC) or dm == 'a':
        return 'Automatic QC'
    return 'Not stated'


def platform_of(*texts):
    t = ' '.join(str(x or '') for x in texts).lower()
    for k, v in (('wave glider', 'Wave Glider'), ('seaexplorer', 'SeaExplorer'), ('sea explorer', 'SeaExplorer'),
                 ('seaglider', 'Seaglider'), ('spray', 'Spray'), ('slocum', 'Slocum')):
        if k in t:
            return v
    return 'Unknown'


def days(a, b):
    try:
        d = (datetime.datetime.fromisoformat(b[:19]) - datetime.datetime.fromisoformat(a[:19])).total_seconds() / 86400
    except ValueError:
        return ''
    return f'{d:.2f}' if 0 <= d <= 400 else 'bad'


# ---------------- per data centre ----------------
def erddap_records(path, keyf):
    groups = collections.defaultdict(list)
    for l in open(path):
        m = json.loads(l)
        if m.get('error'):
            continue
        k = keyf(m)
        if k:
            groups[k].append(m)
    return groups


def row_from_files(source, key, files, delayed_names, extra_platform=''):
    """files: list of dicts with globals, variables/qc, min/max time, title, dataset_id, data_mode."""
    G = [f['globals'] for f in files]
    first = lambda *ks: next((g.get(k) for g in G for k in ks if (g.get(k) or '').strip()), '')
    net = next((tag(g.get('network')) for g in G if tag(g.get('network'))), '')
    starts = [ymd(f.get('start', '')) for f in files if ymd(f.get('start', ''))]
    ends = [f.get('end', '')[:19] for f in files if f.get('end')]
    start_full = min((f.get('start_full', '') for f in files if f.get('start_full')), default='')
    end_full = max(ends, default='')
    qcs = [qc_of(f.get('qc'), f.get('variables')) for f in files if not f.get('unread')]
    present = '1' if any(q[0] == '1' for q in qcs) else '0'
    with_flags = [q for q in qcs if q[0] == '1' and q[2] not in ('Not described', 'None')]
    cf = '1' if with_flags and all(q[1] == '1' for q in with_flags) else '0'
    scheme = with_flags[0][2] if with_flags else ('Not described' if present == '1' else 'None')
    if not qcs:  # only files whose contents couldn't be read (BODC file collections)
        present, cf, scheme = '', '', 'Not read'
    fmts = {fmt_of(g) for g in G}
    modes = [f.get('mode', '') for f in files if f.get('mode')]
    mode = max(modes, key=lambda m: MODE_RANK.get(m, -1)) if modes else ''
    streams = {}
    for f in files:
        s, lv = stream_of(source, f), qc_level_of(f)
        if LEVEL_RANK[lv] > LEVEL_RANK.get(streams.get(s), -1):
            streams[s] = lv
    return {
        'nrt': '1' if 'NRT' in streams else '0', 'recovered': '1' if 'Recovered' in streams else '0',
        'nrt_qc': streams.get('NRT', ''), 'rec_qc': streams.get('Recovered', ''),
        'dm_any': '1' if 'Delayed mode' in streams.values() else '0',
        'source': source, 'deployment': key,
        'institution': canon(first('institution'), source), 'institution_raw': first('institution').strip(),
        'start_date': min(starts) if starts else '', 'start_year': (min(starts) if starts else '')[:4], 'end_date': end_full[:10],
        'glider_days': days(start_full, end_full) if start_full and end_full else '',
        'tagged': '1' if net else '0', 'mission': (net.split('>') + ['', ''])[1].strip() if net else '',
        'region': (net.split('>') + ['', '', ''])[2].strip() if net else '', 'network': net,
        'site': '1' if first('site', 'oceanGliders_site').strip() else '0',
        'wmo_code': next((good_wmo(g.get(k)) for g in G for k in ('wmo_id', 'wmo_platform_code', 'platform_wmo_id', 'wmoid') if good_wmo(g.get(k))), ''),
        'format': min(fmts, key=FMT_RANK.index) if fmts else 'Other', 'og1_available': '1' if 'OG1.0' in fmts else '0',
        'data_mode': mode, 'delayed': '1' if mode in ('D', 'P') or delayed_names else '0',
        'qc_present': present, 'qc_cf': cf, 'qc_scheme': scheme,
        'platform': platform_of(first('platform_type', 'platform_long_name', 'glider_instrument_name', 'glider_model', 'platform_model', 'platform', 'instrument'),
                                ' '.join(f.get('title', '') for f in files), first('summary'), first('source'), extra_platform),
        'datasets': str(len(files)),
        # where the data live, for fetch_tracks.py (written to deployment_refs.jsonl, not the CSV)
        '_refs': [{'id': f.get('dataset_id', ''), 'url': f.get('url', ''), 'stream': stream_of(source, f), 'mode': f.get('mode', ''),
                   'start': f.get('start_full', '') or ymd(f.get('start', '')), 'end': f.get('end', ''), 'variables': f.get('variables') or []}
                  for f in files],
    }


def erddap_mode(src, did, g):
    m = (g.get('data_mode') or '').strip().upper()[:1]
    if m in MODE_RANK:
        return m
    low = did.lower()
    # these data centres use "delayed" for recovered data, so their naming gives P (recovered), not D
    if src == 'IOOS':
        return 'P' if low.endswith('-delayed') else 'R'
    if src == 'VOTO':
        return 'P' if low.startswith(('delayed_', 'og_complete_')) else 'R'
    if src == 'OTN':
        return 'P' if low.endswith('_delayed') else 'R'
    if src == 'BODC':
        return 'D' if did.endswith('_D') else 'P' if did.endswith('_P') else 'R'
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--work', required=True)
    ap.add_argument('--recent', default=(datetime.date.today() - datetime.timedelta(days=730)).isoformat())
    a = ap.parse_args()
    W = lambda f: os.path.join(a.work, f)
    rows, dropped = [], collections.Counter()

    keys = {
        'IOOS': lambda m: m['dataset_id'][:-8] if m['dataset_id'].endswith('-delayed') else m['dataset_id'],
        'VOTO': lambda m: (re.search(r'(SEA\d+_M\d+)', m['dataset_id'], re.I) or [None, None])[1],
        'BODC': lambda m: (re.search(r'deployment (\d+)', m['title'], re.I) or [None, m['dataset_id']])[1],
        'OTN': lambda m: (re.search(r'deployment #(\d+)', m['title'], re.I) or [None, re.sub(r'_(realtime|delayed)$', '', m['dataset_id'])])[1],
    }
    # BODC's OG1 file collections (real time and recovery), keyed by deployment ID; contents unread
    bodc_files = collections.defaultdict(list)
    if os.path.exists(W('BODC_files.jsonl')):
        for l in open(W('BODC_files.jsonl')):
            b = json.loads(l)
            if not b.get('deployment_id'):
                continue
            bodc_files[b['deployment_id']].append({
                'globals': {'institution': b.get('owner', ''), 'wmo_id': b.get('wmo', ''), 'Conventions': 'OG-1.0',
                            'platform_model': b.get('platform_model', '')},
                'qc': {}, 'variables': [], 'unread': True, 'title': b.get('platform_model', ''), 'dataset_id': f"{b['collection']}/{b['folder']}",
                'url': next((f"{ERDDAPS['BODC']}/files/{b['collection']}/{b['folder']}/{n}" for n in b.get('files', []) if n.endswith('.nc')), ''),
                'start': b.get('start', ''), 'start_full': '', 'end': '', 'mode': b['mode']})

    for src, kf in keys.items():
        if not os.path.exists(W(f'{src}_meta.jsonl')):
            continue
        groups = erddap_records(W(f'{src}_meta.jsonl'), kf)
        if src == 'BODC':
            for k in bodc_files:
                groups.setdefault(k, [])
        for k, ms in groups.items():
            files = []
            for m in ms:
                g = m['globals']
                files.append({'globals': g, 'qc': m['qc'], 'variables': m['variables'], 'title': m['title'], 'dataset_id': m['dataset_id'],
                              'url': f"{ERDDAPS[src]}/tabledap/{m['dataset_id']}",
                              'start': m['min_time'] or g.get('time_coverage_start', ''), 'start_full': (m['min_time'] or g.get('time_coverage_start', ''))[:19],
                              'end': (m['max_time'] or g.get('time_coverage_end', ''))[:19], 'mode': erddap_mode(src, m['dataset_id'], g)})
            if src == 'BODC':
                files += bodc_files.get(k, [])
            r = row_from_files(src, k, files, False)
            if r['platform'] == 'Wave Glider':  # surface vehicles, not underwater gliders
                dropped[f'{src} wave gliders'] += 1
                continue
            rows.append(r)

    # Coriolis: JSON + NetCDF headers per deployment folder, times/model/owner from the GDAC trajectory index
    cindex = collections.defaultdict(list)
    if os.path.exists(W('Coriolis_traj_index.txt')):
        lines = [l for l in open(W('Coriolis_traj_index.txt')) if not l.startswith('#')]
        for r in csv.DictReader(lines):
            p = r['file'].strip('/').split('/')
            cindex['/'.join(p[:2])].append(r)

    def ts(s):  # YYYYMMDDHHMMSS -> ISO
        s = (s or '').strip()
        return f'{s[:4]}-{s[4:6]}-{s[6:8]}T{s[8:10]}:{s[10:12]}:{s[12:14]}' if re.match(r'^\d{14}$', s) else ''
    if os.path.exists(W('Coriolis_meta.jsonl')):
        for l in open(W('Coriolis_meta.jsonl')):
            c = json.loads(l)
            ix = cindex.get(c['id'], [])
            # start: the index's deployment_start_date, else the date in the folder name; its time_coverage_start
            # is unreliable (1970/2000 fill values, or the start of an earlier deployment of the same glider)
            fd = re.search(r'_(\d{8})$', c['id'])
            folder_start = ts(fd.group(1) + '000000') if fd else ''
            dep_start = min((ts(r['deployment_start_date']) for r in ix if ts(r['deployment_start_date'])), default='')
            if dep_start and folder_start and abs((datetime.date.fromisoformat(dep_start[:10]) - datetime.date.fromisoformat(folder_start[:10])).days) > 60:
                dep_start = folder_start  # index start disagrees with the folder's own date by > 60 days
            ix_start = (dep_start or folder_start
                        or min((ts(r['time_coverage_start']) for r in ix if ts(r['time_coverage_start'])), default=''))
            ix_end = max((ts(r['time_coverage_end']) for r in ix if ts(r['time_coverage_end'])), default='')
            if not c.get('owner'):
                c['owner'] = next((r['owning_institution'] for r in ix if r['owning_institution'].strip()), '')
            if not good_wmo(c.get('wmo')):
                c['wmo'] = next((r['wmo'] for r in ix if good_wmo(r['wmo'])), '')
            files = []
            for n, h in c.get('nc', {}).items():
                h = dict(h)
                if ix_start:
                    h['t_first'] = ix_start.replace('T', ' ')
                h.setdefault('t_last', ix_end.replace('T', ' '))
                g = dict(h.get('globals', {}))
                if c.get('owner') and not g.get('institution'):
                    g['institution'] = c['owner']
                g.setdefault('wmo_platform_code', c.get('wmo', ''))
                if c.get('json_network') and not g.get('network'):
                    g['network'] = c['json_network']
                s = h.get('t_first') or g.get('time_coverage_start', '')
                e = h.get('t_last') or g.get('time_coverage_end', '')
                files.append({'globals': g, 'qc': h.get('qc', {}), 'variables': [], 'title': '', 'dataset_id': n, 'url': CORIOLIS + c['id'] + '/' + n,
                              'start': s or c.get('start', ''), 'start_full': s[:19], 'end': e[:19],
                              'mode': (g.get('data_mode') or ('D' if re.search(r'_D\.nc$', n) else 'R'))[:1].upper()})
            if not files:  # no readable file: JSON and index only
                files = [{'globals': {'institution': c.get('owner', ''), 'wmo_platform_code': c.get('wmo', ''), 'network': c.get('json_network', '')},
                          'qc': {}, 'variables': [], 'title': '', 'dataset_id': c['id'], 'start': ix_start or c.get('start', ''),
                          'url': next((CORIOLIS + c['id'] + '/' + n for n in c.get('files', []) if n.endswith('.nc')), ''),
                          'start_full': ix_start[:19], 'end': ix_end[:19], 'mode': ''}]
            plat = ' '.join([c.get('platform_model', '')] + [r['glider_model'] for r in ix] +
                            [h.get(k, '') for h in c.get('nc', {}).values() for k in ('PLATFORM_TYPE', 'PLATFORM_FAMILY', 'PLATFORM_MAKER')])
            rows.append(row_from_files('Coriolis', c['id'], files, False, plat))

    # IMOS: one header per deployment folder
    if os.path.exists(W('IMOS_meta.jsonl')):
        for l in open(W('IMOS_meta.jsonl')):
            m = json.loads(l)
            g = m.get('globals', {})
            files = [{'globals': g, 'qc': m.get('qc', {}), 'variables': [], 'title': g.get('title', ''), 'dataset_id': m['file'], 'url': IMOS_S3 + m['file'],
                      'start': g.get('time_coverage_start', ''), 'start_full': g.get('time_coverage_start', '')[:19],
                      'end': g.get('time_coverage_end', '')[:19], 'mode': 'R' if '/REALTIME/' in m['id'] else 'D'}]
            rows.append(row_from_files('IMOS', m['id'].replace('IMOS/ANFOG/', ''), files, False,
                                       'seaglider' if '/seaglider/' in m['id'] else 'slocum'))

    for r in rows:
        r['recent'] = '1' if (r['end_date'] or r['start_date']) >= a.recent else '0'
        r['wmo'] = '1' if r['wmo_code'] else '0'

    # same deployment on more than one data centre: WMO ID and start date within 3 days
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    by_wmo = collections.defaultdict(list)
    for i, r in enumerate(rows):
        if r['wmo_code'] and r['start_date']:
            by_wmo[r['wmo_code']].append(i)
    for idx in by_wmo.values():
        for x in idx:
            for y in idx:
                if x < y and rows[x]['source'] != rows[y]['source']:
                    dx = datetime.date.fromisoformat(rows[x]['start_date'])
                    dy = datetime.date.fromisoformat(rows[y]['start_date'])
                    if abs((dx - dy).days) <= 3:
                        parent[find(x)] = find(y)
    groups = collections.defaultdict(list)
    for i in range(len(rows)):
        groups[find(i)].append(i)
    for gid, idx in groups.items():
        srcs = sorted({rows[i]['source'] for i in idx})
        for i in idx:
            rows[i]['group'] = f'g{gid}'
            rows[i]['also_in'] = ' '.join(s for s in srcs if s != rows[i]['source'])

    cols = ['source', 'deployment', 'institution', 'institution_raw', 'start_date', 'start_year', 'end_date', 'glider_days',
            'tagged', 'mission', 'region', 'network', 'site', 'wmo', 'wmo_code', 'format', 'og1_available', 'data_mode', 'delayed',
            'qc_present', 'qc_cf', 'qc_scheme', 'nrt', 'recovered', 'nrt_qc', 'rec_qc', 'dm_any', 'platform', 'recent', 'datasets', 'group', 'also_in']
    with open(W('glider_deployments.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    with open(W('deployment_refs.jsonl'), 'w') as f:
        for r in rows:
            f.write(json.dumps({'dep_id': f"{r['source']}:{r['deployment']}", 'source': r['source'], 'deployment': r['deployment'],
                                'refs': r['_refs']}) + '\n')

    print('rows', len(rows), 'unique', len(groups), 'dropped', dict(dropped))
    for s in ('IOOS', 'VOTO', 'BODC', 'OTN', 'Coriolis', 'IMOS'):
        R = [r for r in rows if r['source'] == s]
        if not R:
            continue
        c = lambda k: dict(collections.Counter(r[k] for r in R).most_common(6))
        gd = [float(r['glider_days']) for r in R if r['glider_days'] not in ('', 'bad')]
        print(f'== {s}: {len(R)} tagged {sum(r["tagged"] == "1" for r in R)} glider-days {sum(gd):.0f} (missing {sum(r["glider_days"] == "" for r in R)}, bad {sum(r["glider_days"] == "bad" for r in R)})')
        for k in ('format', 'data_mode', 'qc_present', 'qc_scheme', 'platform'):
            print('   ', k, c(k))
        for k in ('nrt_qc', 'rec_qc'):
            print('   ', k, c(k))


if __name__ == '__main__':
    main()
