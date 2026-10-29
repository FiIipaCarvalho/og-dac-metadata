"""Build a standalone copy of the dashboard for GitHub Pages: docs/index.html + docs/data/.

    python3 make_standalone.py

dashboard/index.html is written for the claude.ai Dashboard artifact type, which supplies a `dash`
object (datasets, calcs, colours, theme tokens). This wraps the same page with a small stand-in for
that object, loading the CSV/JSON snapshot from docs/data/, so it runs as a plain static page.
"""
import pathlib
import shutil

ROOT = pathlib.Path(__file__).parent
page = (ROOT / 'dashboard' / 'index.html').read_text()
(ROOT / 'docs' / 'data').mkdir(parents=True, exist_ok=True)
for f in ['glider_deployments.csv', 'spot_checks.json'] + sorted(p.name for p in (ROOT / 'data').glob('tracks_*_*.png')):
    shutil.copy(ROOT / 'data' / f, ROOT / 'docs' / 'data' / f)

SHIM = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OceanGliders data centre metadata</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Source+Serif+4:wght@400;600&display=swap">
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>
<style>
  /* stand-ins for the claude.ai dashboard theme tokens */
  :root {
    --font-anthropic-sans: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
    --font-anthropic-serif: "Source Serif 4", Georgia, serif;
    --color-fg: #1f2328; --color-fg-muted: #5c6670; --color-bg: #f7f8fa; --color-panel: #ffffff;
    --color-border-line: #dde1e6; --color-ok: #1f8a4c; --color-warn: #c98a00; --color-bad: #c43c3c;
    --cds-text-secondary: #5c6670; --cds-surface-popover: #ffffff; --cds-border: #dde1e6;
    --cds-shadow-popover: 0 4px 16px rgba(0,0,0,.12);
    --cds-chart-grid: #eceff2; --cds-chart-axis: #b8bfc7; --cds-chart-muted: #c3c9d0; --cds-chart-reference: #8a939c;
    --cds-font-size-caption: .8rem; --cds-font-size-body: .95rem; --cds-font-size-heading: 1.05rem; --cds-font-size-title: 1.6rem;
    --cds-font-weight-medium: 600; --cds-radius: 8px;
    --cds-gap-xs: 4px; --cds-gap-sm: 8px; --cds-gap-md: 12px; --cds-gap-lg: 20px; --cds-gap-xl: 32px;
    --cds-pad-xs: 4px; --cds-pad-sm: 8px; --cds-pad-md: 12px; --cds-pad-lg: 16px;
    --cds-dur-slow: .3s; --cds-ease-out: ease-out;
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --color-fg: #e6e8eb; --color-fg-muted: #9aa4ae; --color-bg: #15181c; --color-panel: #1d2126;
      --color-border-line: #2e343b; --color-ok: #3fb872; --color-warn: #e0a520; --color-bad: #e06363;
      --cds-text-secondary: #9aa4ae; --cds-surface-popover: #1d2126; --cds-border: #2e343b;
      --cds-chart-grid: #262b31; --cds-chart-axis: #4a525b; --cds-chart-muted: #4b535c;
      color-scheme: dark;
    }
  }
  body { margin: 0; background: var(--color-bg); color: var(--color-fg); font-family: var(--font-anthropic-sans); }
  #dash-root { padding: 24px clamp(16px, 4vw, 48px); max-width: 1400px; margin: 0 auto; }
  .dash-skeleton { color: transparent !important; background: var(--color-border-line); border-radius: 4px; }
  a { color: inherit; }
</style>
<script>
/* minimal stand-in for the claude.ai `dash` object: datasets, calcs, onData, colours, links */
(function () {
  const DATASETS = {
    deployments: () => fetch('data/glider_deployments.csv').then(r => r.text()).then(t => d3.csvParse(t)),
    spot_checks: () => fetch('data/spot_checks.json').then(r => r.json())
  };
  const store = {}, calcs = {}, cache = {}, listeners = [];
  const status = (id) => store[id] ? store[id] : { status: 'loading', data: [] };
  function calcResult(id) {
    if (cache[id]) return cache[id];
    const c = calcs[id];
    const ins = c.inputs.map(i => window.dash.data(i));
    if (ins.some(x => x.status !== 'ok')) return { status: ins.find(x => x.status !== 'ok').status, data: [] };
    try { cache[id] = { status: 'ok', data: c.fn(...ins.map(x => x.data)) }; }
    catch (e) { console.error(id, e); cache[id] = { status: 'error', data: [], message: String(e) }; }
    return cache[id];
  }
  const fire = () => listeners.forEach(fn => { try { fn(); } catch (e) { console.error(e); } });
  window.dash = {
    data: (id) => calcs[id] ? calcResult(id) : status(id),
    calc: (id, spec) => { calcs[id] = spec; },
    loader: () => {},
    onData: (fn) => { listeners.push(fn); try { fn(); } catch (e) { console.error(e); } },
    colors: ['#2f6fd0', '#e0752d', '#2a9d8f', '#c2410c', '#7c5cc4', '#b5447e', '#5b8c3a', '#d4a017'],
    refresh: () => {}, params: () => ({}), setParams: () => {}, resetParams: () => {},
    setLink: (name) => { try { history.replaceState(null, '', '#' + name); } catch (e) {} }
  };
  Promise.all(Object.entries(DATASETS).map(([id, load]) => load()
    .then(d => { store[id] = { status: 'ok', data: d }; })
    .catch(e => { store[id] = { status: 'error', data: [], message: String(e) }; })))
    .then(fire);
  let w = window.innerWidth;
  window.addEventListener('resize', () => { if (window.innerWidth !== w) { w = window.innerWidth; fire(); } });
})();
</script>
</head>
<body>
<div id="dash-root">
"""
(ROOT / 'docs' / 'index.html').write_text(SHIM + page + "\n</div>\n</body>\n</html>\n")
print('wrote docs/index.html')
