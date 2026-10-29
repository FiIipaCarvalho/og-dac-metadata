"""Shared settings and helpers for the OceanGliders tag-adoption harvest."""
import io
import time
import urllib.request

ERDDAPS = {
    'IOOS': 'https://gliders.ioos.us/erddap',
    'VOTO': 'https://erddap.observations.voiceoftheocean.org/erddap',
    'BODC': 'https://linkedsystems.uk/erddap',
    'OTN': 'https://erddap.oceantrack.org/erddap',
}
CORIOLIS = 'https://data-oceangliders.ifremer.fr/'
IMOS_S3 = 'https://imos-data.s3-ap-southeast-2.amazonaws.com/'

# attributes that describe a flag variable (CF)
FLAG_ATTRS = ('flag_values', 'flag_meanings')
# attributes kept per variable, for the parameter inventory (sensors.py)
VAR_ATTRS = ('standard_name', 'long_name', 'units')


def get(url, timeout=90, tries=5, text=True):
    for k in range(tries):
        try:
            b = urllib.request.urlopen(url, timeout=timeout).read()
            return b.decode(errors='ignore') if text else b
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(3 + 4 * k)


class RangeFile(io.RawIOBase):
    """Read-only file over HTTP range requests, cached in 256 KB blocks (enough for h5py to read headers)."""
    B = 1 << 18

    def __init__(self, url):
        self.url, self.pos, self.cache = url, 0, {}
        req = urllib.request.Request(url, method='HEAD')
        self.size = int(urllib.request.urlopen(req, timeout=60).headers['Content-Length'])

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def _block(self, i):
        if i not in self.cache:
            a, b = i * self.B, min(self.size, (i + 1) * self.B) - 1
            req = urllib.request.Request(self.url, headers={'Range': f'bytes={a}-{b}'})
            self.cache[i] = urllib.request.urlopen(req, timeout=90).read()
        return self.cache[i]

    def readinto(self, buf):
        n = min(len(buf), self.size - self.pos)
        if n <= 0:
            return 0
        out, p = bytearray(), self.pos
        while len(out) < n:
            blk = self._block(p // self.B)
            o = p % self.B
            chunk = blk[o:o + n - len(out)]
            out += chunk
            p += len(chunk)
        buf[:n] = out
        self.pos += n
        return n


def magic(url):
    """First 8 bytes of a remote file: b'\\x89HDF...' for NetCDF4/HDF5, b'CDF...' for classic NetCDF."""
    req = urllib.request.Request(url, headers={'Range': 'bytes=0-7'})
    return urllib.request.urlopen(req, timeout=60).read()[:8]


def remote_header(url, time_var=('JULD', 'TIME', 'time')):
    """Pick the right reader from the file signature (netCDF4's byte-range open of an HDF5 file can
    sit through long libcurl timeouts before failing)."""
    if magic(url).startswith(b'\x89HDF'):
        return h5_header(url, time_var)
    return nc3_header(url, time_var)


def _s(v):
    if isinstance(v, bytes):
        v = v.decode(errors='ignore')
    if hasattr(v, 'tolist'):
        v = v.tolist()
    if isinstance(v, list):
        v = ', '.join(x.decode(errors='ignore') if isinstance(x, bytes) else str(x) for x in v)
    return str(v)


def h5_header(url, time_var=('JULD', 'TIME', 'time')):
    """Global attributes, variable names, flag-variable attributes and (for EGO-style files) first/last
    valid time and platform variables of a remote NetCDF4/HDF5 file."""
    import h5py
    import numpy as np
    with h5py.File(RangeFile(url), 'r') as h:
        g = {k: _s(v)[:300] for k, v in h.attrs.items()}
        names, qc, extra = [], {}, {}
        for name, obj in h.items():
            if isinstance(obj, h5py.Dataset):
                names.append(name)
                fa = {a: _s(obj.attrs[a])[:300] for a in FLAG_ATTRS if a in obj.attrs}
                if fa:
                    qc[name] = fa
        for tv in time_var:
            if tv not in h or not h[tv].size or 'units' not in h[tv].attrs:
                continue
            v, fill = h[tv], h[tv].attrs.get('_FillValue')
            ok = lambda a: a[np.isfinite(a) & ((a != fill) if fill is not None else True) & (np.abs(a) < 1e12)]
            head = tail = np.array([])
            for w in (3, 50):
                head = ok(np.asarray(v[:w], dtype=float)) if not head.size else head
                tail = ok(np.asarray(v[-w:], dtype=float)) if not tail.size else tail
                if head.size and tail.size:
                    break
            if head.size and tail.size:
                import cftime
                t0, t1 = cftime.num2date([head.min(), tail.max()], _s(v.attrs['units']), only_use_cftime_datetimes=False)
                extra['t_first'], extra['t_last'], extra['t_var'] = str(t0)[:19], str(t1)[:19], tv
                break
        for cv in ('PLATFORM_TYPE', 'PLATFORM_FAMILY', 'PLATFORM_MAKER', 'GLIDER_OWNER', 'OPERATING_INSTITUTION', 'DATA_CENTRE'):
            if cv in h:
                try:
                    a = h[cv][()]
                    extra[cv] = (a.tobytes() if hasattr(a, 'tobytes') else bytes(a)).decode(errors='ignore').replace('\x00', '').strip()[:120]
                except Exception:
                    pass
    return g, names, qc, extra


def nc3_header(url, time_var=('JULD', 'TIME', 'time')):
    """Same for any file netCDF4 can open by byte range (NetCDF3 classic, e.g. EGO), plus first/last time."""
    import netCDF4
    ds = netCDF4.Dataset(url + '#mode=bytes')
    try:
        g = {k: _s(v)[:300] for k, v in ds.__dict__.items()}
        names, qc = list(ds.variables), {}
        for name, v in ds.variables.items():
            fa = {a: _s(getattr(v, a))[:300] for a in FLAG_ATTRS if hasattr(v, a)}
            if fa:
                qc[name] = fa
        extra = {}
        for tv in time_var:
            if tv in ds.variables and ds.variables[tv].size:
                v = ds.variables[tv]
                try:
                    import numpy as np
                    # first/last valid times: edge values can be fill values
                    # read 3 values at each end first (each can be its own HTTP request), widen only if all are fill
                    head = tail = np.array([])
                    for w in (3, 50):
                        head = np.ma.compressed(np.ma.masked_invalid(v[:w])) if not head.size else head
                        tail = np.ma.compressed(np.ma.masked_invalid(v[-w:])) if not tail.size else tail
                        if head.size and tail.size:
                            break
                    if not (head.size and tail.size):
                        continue  # EGO files can leave TIME as fill values and fill JULD; try the next one
                    t0, t1 = netCDF4.num2date([head.min(), tail.max()], v.units)
                    extra['t_first'], extra['t_last'], extra['t_var'] = str(t0)[:19], str(t1)[:19], tv
                except Exception as e:
                    extra['t_err'] = str(e)[:80]
                break
        for cv in ('PLATFORM_TYPE', 'PLATFORM_FAMILY', 'PLATFORM_MAKER', 'GLIDER_OWNER', 'OPERATING_INSTITUTION', 'DATA_CENTRE'):
            if cv in ds.variables:
                try:
                    extra[cv] = str(netCDF4.chartostring(ds.variables[cv][:])).strip()[:120]
                except Exception:
                    pass
        return g, names, qc, extra
    finally:
        ds.close()
