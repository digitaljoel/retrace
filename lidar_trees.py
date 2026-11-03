"""Real trees from a lidar point cloud, for make_viewer.py --trees.

    python lidar_trees.py --dem lidar/manele_lidar.tif --align lidar/manele_align.json \
        --ept https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/entwine/geoid12b/10331 \
        [--osm osm/manele_cover.json --osm-shift osm/manele_shift.json] --out lidar/manele_trees.json

Streams the survey's points over the area of the --dem file from an Entwine point-tile (EPT) index,
only as deep as tree-finding needs. Everything the survey didn't call ground, minus the ground
itself, gives canopy height; buildings (from OpenStreetMap, if given) are masked out. Each treetop is
a local high point of the canopy, its crown the patch of canopy draining to it. The output lists every
tree in GSPro's coordinates with its height, crown radius and a shape (columnar or spreading).
Needs numpy, scipy, rasterio and laspy with LAZ support (pip install "laspy[lazrs]"); surveys stored in a
different projection from the elevation model (USGS's are in web-map coordinates) also need pyproj.

    python lidar_trees.py --make-dem lidar/hobblecreek_lidar.tif --lat 40.16083 --lon -111.51211 \
        --ept https://usgs-lidar-public.s3.us-west-2.amazonaws.com/USGS_LPC_UT_Central_QL1_B2_2018_LAS_2019

builds the elevation model itself from the survey's ground points (1 m, UTM metres), for courses with
no separate elevation download; then run align and trees against it as usual.
"""
import argparse, io, json, math, sys, time, urllib.request
import concurrent.futures as cf
import numpy as np
from scipy import ndimage as ndi

UA = {"User-Agent": "retrace/0.1 (golf replay viewer)"}


def get(url, tries=5):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return r.read()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 * (k + 1))


def ept_nodes(base, box, depth):
    """Keys of every point tile down to `depth` that overlaps box = (E0, N0, E1, N1)."""
    ept = json.loads(get(base + "/ept.json"))
    b = ept["bounds"]; W = b[3] - b[0]
    keys = []

    def overlaps(key):
        d, x, y, _ = map(int, key.split("-"))
        s = W / 2 ** d; e0 = b[0] + x * s; n0 = b[1] + y * s
        return d <= depth and e0 <= box[2] and e0 + s >= box[0] and n0 <= box[3] and n0 + s >= box[1]

    def walk(hkey):
        for key, cnt in json.loads(get(f"{base}/ept-hierarchy/{hkey}.json")).items():
            if not overlaps(key):
                continue
            if cnt == -1:
                walk(key)          # this node's subtree lives in its own hierarchy file
            elif cnt > 0:
                keys.append(key)
    walk("0-0-0-0")
    return sorted(set(keys)), W


def ept_crs(base):
    srs = json.loads(get(base + "/ept.json")).get("srs") or {}
    return f"EPSG:{srs['horizontal']}" if srs.get("horizontal") else None


def box_in(box, src_crs, dst_crs, pad=30):
    """A box given in src_crs, as a (padded) box in dst_crs."""
    if not dst_crs or not src_crs or dst_crs == src_crs:
        return box
    from pyproj import Transformer
    t = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
    xs = np.linspace(box[0], box[2], 9); ys = np.linspace(box[1], box[3], 9)
    gx, gy = np.meshgrid(xs, ys); X, Y = t.transform(gx.ravel(), gy.ravel())
    return (X.min() - pad, Y.min() - pad, X.max() + pad, Y.max() + pad)


def read_points(base, keys, box, consume, to_grid=None):
    """Fetch tiles in parallel; hand each one's points (inside box, not withheld) to consume() in turn,
    so only a tile's worth of points is ever held at once. box is in the survey's own coordinates;
    to_grid (optional) maps them into the elevation model's."""
    import laspy

    def one(key):
        las = laspy.read(io.BytesIO(get(f"{base}/ept-data/{key}.laz")))
        E, N, Z = np.asarray(las.x), np.asarray(las.y), np.asarray(las.z, dtype=np.float32)
        cls = np.asarray(las.classification)
        keep = (E >= box[0]) & (E <= box[2]) & (N >= box[1]) & (N <= box[3])
        try:
            keep &= ~np.asarray(las.withheld, dtype=bool)
        except Exception:
            pass
        return E[keep], N[keep], Z[keep], cls[keep]
    n = 0
    with cf.ThreadPoolExecutor(16) as ex:
        for k, (E, N, Z, C) in enumerate(ex.map(one, keys)):
            if to_grid is not None and len(E):
                E, N = to_grid(E, N)
            consume(E, N, Z, C); n += len(E)
            if (k + 1) % 250 == 0:
                print(f"  {k + 1}/{len(keys)} tiles", flush=True)
    return n


def survey_reader(ept, grid_crs, box):
    """Tile keys, the survey-side box and a point mapper, so a survey in another projection
    (USGS: web-map metres) lands on a grid in true metres."""
    s_crs = ept_crs(ept)
    to_grid = None
    if s_crs and grid_crs and s_crs != grid_crs:
        from pyproj import Transformer
        t = Transformer.from_crs(s_crs, grid_crs, always_xy=True)
        to_grid = lambda E, N: tuple(np.asarray(v) for v in t.transform(E, N))
    sbox = box_in(box, grid_crs, s_crs)
    return s_crs, sbox, to_grid


def depth_for(ept, sbox, density):
    eb = json.loads(get(ept + "/ept.json"))["bounds"]
    return max(0, round(math.log2((eb[3] - eb[0]) / math.sqrt(1.33 * 128 * 128 / density))))


def make_dem(a):
    """An elevation model from the survey's ground points: 1 m cells in UTM metres around --lat/--lon."""
    import rasterio
    from rasterio.transform import from_origin
    from pyproj import Transformer
    zone = int((a.lon + 180) // 6) + 1
    crs = f"EPSG:{32600 + zone if a.lat >= 0 else 32700 + zone}"
    cx, cy = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(a.lon, a.lat)
    res = a.res
    box = (math.floor(cx - a.half_width), math.floor(cy - a.half_height), math.ceil(cx + a.half_width), math.ceil(cy + a.half_height))
    w, h = int((box[2] - box[0]) / res), int((box[3] - box[1]) / res)
    s_crs, sbox, to_grid = survey_reader(a.ept, crs, box)
    keys, _ = ept_nodes(a.ept, sbox, depth_for(a.ept, sbox, a.density))
    print(f"ground model {w} x {h} cells at {res:g} m in {crs}; reading {len(keys)} point tiles", flush=True)
    S = np.zeros((h, w)); Ncnt = np.zeros((h, w))

    def consume(E, N, Z, C):
        g = C == 2
        if not g.any():
            return
        ix = ((E[g] - box[0]) / res).astype(int); iy = ((N[g] - box[1]) / res).astype(int)
        ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
        np.add.at(S, (iy[ok], ix[ok]), Z[g][ok]); np.add.at(Ncnt, (iy[ok], ix[ok]), 1)
    t0 = time.time()
    n = read_points(a.ept, keys, sbox, consume, to_grid)
    have = Ncnt > 0
    print(f"{n:,} points in {time.time() - t0:.0f} s; ground in {have.mean() * 100:.0f}% of cells", flush=True)
    Zg = np.where(have, S / np.maximum(Ncnt, 1), 0.0)
    # fill cells with no ground return (under dense trees, roofs) from the ground around them
    for sig in (1.5, 3, 6, 12, 24, 48):
        if have.all():
            break
        num = ndi.gaussian_filter(np.where(have, Zg, 0), sig); den = ndi.gaussian_filter(have.astype(float), sig)
        fill = ~have & (den > 0.05)
        Zg[fill] = num[fill] / den[fill]; have = have | fill
    Zg[~have] = -999999.0
    prof = dict(driver="GTiff", width=w, height=h, count=1, dtype="float32", crs=crs, nodata=-999999.0,
                transform=from_origin(box[0], box[3], res, res), compress="deflate", predictor=3, tiled=True, blockxsize=512, blockysize=512)
    out = np.round(Zg[::-1], 2).astype(np.float32)          # north row first
    with rasterio.open(a.make_dem, "w", **prof) as dst:
        dst.write(out, 1)
        dst.update_tags(SOURCE=f"ground points (class 2) of {a.ept}, {res:g} m cells, gaps filled from surrounding ground")
    print(f"wrote {a.make_dem}: elevation {out[out > -1000].min():.1f} to {out.max():.1f} m")


def building_mask(osm_files, shift, align, grid, crs="EPSG:6634"):
    """Rasterised OSM building footprints (+2 m) on the canopy grid."""
    from PIL import Image, ImageDraw
    from rasterio.warp import transform
    E0, N0, res, w, h = grid
    img = Image.new("1", (w, h), 0); dr = ImageDraw.Draw(img)
    o = align["orientation"]; th = align["theta"]
    # the OSM shift is in GSPro x/z; turn it into a map-grid offset
    sx, sz = shift
    u, v = (sz * o["sz"], sx * o["sx"]) if o["swap"] else (sx * o["sx"], sz * o["sz"])
    dE = align["scale"] * (math.cos(th) * u - math.sin(th) * v); dN = align["scale"] * (math.sin(th) * u + math.cos(th) * v)
    n = 0
    for f in osm_files:
        for e in json.load(open(f, encoding="utf-8"))["elements"]:
            if "building" not in e.get("tags", {}) or e["type"] != "way" or not e.get("geometry"):
                continue
            lon = [q["lon"] for q in e["geometry"]]; lat = [q["lat"] for q in e["geometry"]]
            Es, Ns = transform("EPSG:4326", crs, lon, lat)
            dr.polygon([((x + dE - E0) / res, (N0 + h * res - (y + dN)) / res) for x, y in zip(Es, Ns)], fill=1)
            n += 1
    m = np.asarray(img, dtype=bool)[::-1]                     # row 0 = south
    return ndi.binary_dilation(m, iterations=int(round(2 / res))), n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dem"); ap.add_argument("--align")
    ap.add_argument("--make-dem", help="build an elevation model from the survey's ground points instead (needs --lat/--lon)")
    ap.add_argument("--lat", type=float); ap.add_argument("--lon", type=float)
    ap.add_argument("--half-width", type=float, default=1800); ap.add_argument("--half-height", type=float, default=1400)
    ap.add_argument("--res", type=float, default=1.0)
    ap.add_argument("--ept", required=True, help="Entwine point-tile index URL (folder holding ept.json)")
    ap.add_argument("--osm", action="append", default=[]); ap.add_argument("--osm-shift")
    ap.add_argument("--out")
    ap.add_argument("--density", type=float, default=2.0, help="rough density target; the index often holds more (default 2)")
    ap.add_argument("--min-height", type=float, default=3.0, help="shortest tree to keep, metres")
    ap.add_argument("--preview", help="also save a PNG of canopy heights with the trees found")
    a = ap.parse_args()
    if a.make_dem:
        if a.lat is None or a.lon is None:
            ap.error("--make-dem needs --lat and --lon")
        return make_dem(a)
    if not (a.dem and a.align and a.out):
        ap.error("--dem, --align and --out are needed to find trees")
    import rasterio
    from scipy.ndimage import map_coordinates
    al = json.load(open(a.align))

    with rasterio.open(a.dem) as src:
        D = src.read(1).astype(np.float64); nod = src.nodata; tr = src.transform
        box = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
        grid_crs = f"EPSG:{src.crs.to_epsg()}" if src.crs and src.crs.to_epsg() else None
    D[(D == nod) | (D < -1000)] = -999.0
    sample = lambda G, E, N: map_coordinates(G, [(N - tr.f) / tr.e - 0.5, (E - tr.c) / tr.a - 0.5], order=1, mode="nearest")
    ground_at = lambda E, N: sample(D, E, N)
    # canopy is measured above the highest ground within ~2 m, so returns off a cliff face (below the
    # cliff top) don't read as trees; on ordinary ground it changes little
    Dtop = ndi.maximum_filter(D, size=int(round(3.5 / abs(tr.a))) | 1)
    top_at = lambda E, N: sample(Dtop, E, N)

    # how deep to read: each tile holds about 128 x 128 points across its width
    s_crs, sbox, to_grid = survey_reader(a.ept, grid_crs, box)
    want = depth_for(a.ept, sbox, a.density)
    keys, W = ept_nodes(a.ept, sbox, want)
    print(f"reading {len(keys)} point tiles down to depth {want}", flush=True)
    # canopy height model: the tallest non-ground return in each 1 m cell, above the ground under it
    res = 1.0
    w = int((box[2] - box[0]) / res); h = int((box[3] - box[1]) / res)
    CHM = np.zeros((h, w), np.float32); dzs = []
    rng = np.random.default_rng(3)

    def consume(E, N, Z, C):
        g = C == 2
        if g.any():   # a sample of ground points, to check the two surveys' heights agree
            pick = rng.choice(np.flatnonzero(g), min(400, int(g.sum())), replace=False)
            dzs.append(np.c_[E[pick], N[pick], Z[pick] - ground_at(E[pick], N[pick])])
        v = (C == 1) | (C == 0)
        if not v.any():
            return
        Hc = Z[v] - top_at(E[v], N[v])
        ok = (Hc > 0.5) & (Hc < 70)
        ix = ((E[v][ok] - box[0]) / res).astype(int).clip(0, w - 1); iy = ((N[v][ok] - box[1]) / res).astype(int).clip(0, h - 1)
        np.maximum.at(CHM, (iy, ix), Hc[ok].astype(np.float32))
    t0 = time.time()
    n_pts = read_points(a.ept, keys, sbox, consume, to_grid)
    area = (box[2] - box[0]) * (box[3] - box[1])
    print(f"{n_pts:,} points ({n_pts / area:.1f} per m2) in {time.time() - t0:.0f} s", flush=True)
    # the point cloud and the elevation model can use different height references (ellipsoid vs geoid):
    # fit the gap as a gently tilted plane from the ground points and take it off
    G_ = np.concatenate(dzs); G_ = G_[np.abs(G_[:, 2] - np.median(G_[:, 2])) < 3]
    Am = np.c_[np.ones(len(G_)), G_[:, 0] - box[0], G_[:, 1] - box[1]]
    coef = np.linalg.lstsq(Am, G_[:, 2], rcond=None)[0]
    resid = G_[:, 2] - Am @ coef
    print(f"ground points sit {np.median(G_[:, 2]):+.2f} m from the elevation model; after a tilted-plane fit, "
          f"median gap {np.median(np.abs(resid)):.2f} m")
    xs = box[0] + (np.arange(w) + 0.5) * res - box[0]; ys = box[1] + (np.arange(h) + 0.5) * res - box[1]
    lift = coef[0] + coef[1] * xs[None, :] + coef[2] * ys[:, None]
    CHM = np.where(CHM > 0, np.maximum(CHM - lift, 0), 0).astype(np.float64)
    CHM = np.maximum(CHM, ndi.grey_closing(CHM, size=3) * (CHM == 0))      # fill single-cell gaps
    if a.osm:
        shift = json.load(open(a.osm_shift))["shift"] if a.osm_shift else (0.0, 0.0)
        bm, nb = building_mask(a.osm, shift, al, (box[0], box[1], res, w, h), grid_crs or "EPSG:6634")
        CHM[bm] = 0
        print(f"masked {nb} OSM buildings")

    # treetops: local highs, with a search window that grows with the tree
    S = ndi.gaussian_filter(CHM, 0.8)
    tops = np.zeros_like(S, bool)
    for r_, lo, hi in ((1.5, 0, 6), (2.5, 6, 12), (3.5, 12, 20), (4.5, 20, 99)):
        yy, xx = np.mgrid[-math.ceil(r_):math.ceil(r_) + 1, -math.ceil(r_):math.ceil(r_) + 1]
        fp = xx ** 2 + yy ** 2 <= r_ ** 2
        tops |= (S == ndi.maximum_filter(S, footprint=fp)) & (S >= lo) & (S < hi)
    tops &= S >= a.min_height
    lab, n = ndi.label(tops)
    cy, cx = np.array(ndi.center_of_mass(tops, lab, range(1, n + 1))).T
    # crowns: canopy cells drain to the nearest-uphill treetop (watershed on inverted canopy height)
    markers = np.zeros(S.shape, np.int32); markers[np.round(cy).astype(int), np.round(cx).astype(int)] = np.arange(1, n + 1)
    canopy = S >= 1.5
    markers[~canopy & (markers == 0)] = -1
    inv = (np.clip(70 - S, 0, 70) * 900).astype(np.uint16)
    crowns = ndi.watershed_ift(inv, markers)
    crowns[~canopy] = 0
    area_px = ndi.sum(np.ones_like(S), crowns, range(1, n + 1))
    top_h = ndi.maximum(CHM, crowns, range(1, n + 1))
    r = np.sqrt(np.maximum(area_px, 1) * res * res / math.pi)
    r = np.clip(r, 0.8, np.maximum(1.2, 0.9 * top_h))
    Ec = box[0] + (cx + 0.5) * res; Nc = box[1] + (cy + 0.5) * res
    base = ground_at(Ec, Nc)
    k6 = int(round(6 / abs(tr.a))) | 1
    relief = sample(ndi.maximum_filter(D, size=k6) - ndi.minimum_filter(np.where(D < -900, 1e4, D), size=k6), Ec, Nc)
    # nothing standing in the surf or at the waterline, nor on a cliff (ground falling 8+ m within 6 m)
    keep = (top_h >= a.min_height) & (base > 3.0) & (relief < 8.0)
    Et, Nt = Ec[keep], Nc[keep]
    ht, rt = top_h[keep], r[keep]
    kind = ((ht >= 10) & (rt <= 0.26 * ht)).astype(int)        # 1 = tall and narrow (columnar), 0 = spreading

    # map grid -> GSPro x/z
    o = al["orientation"]; c, s = math.cos(-al["theta"]), math.sin(-al["theta"])
    dE, dN = (Et - al["tE"]) / al["scale"], (Nt - al["tN"]) / al["scale"]
    u = al["cu"] + c * dE - s * dN; v = al["cv"] + s * dE + c * dN
    x, z = (v * o["sx"], u * o["sz"]) if o["swap"] else (u * o["sx"], v * o["sz"])
    out = {"source": a.ept, "n": int(keep.sum()), "x": np.round(x, 1).tolist(), "z": np.round(z, 1).tolist(),
           "h": np.round(ht, 1).tolist(), "r": np.round(rt, 1).tolist(), "kind": kind.tolist()}
    json.dump(out, open(a.out, "w"), separators=(",", ":"))
    if a.preview:   # canopy heights with the detected trees, for a quick look
        from PIL import Image, ImageDraw
        v = np.clip(CHM / 20, 0, 1)[::-1]
        im = Image.fromarray((np.stack([v * 40 + 25, v * 200 + 25, v * 60 + 25], -1)).astype(np.uint8)).convert("RGB")
        d = ImageDraw.Draw(im)
        for e_, n_, r_, k_ in zip(Et, Nt, rt, kind):
            px, py = (e_ - box[0]) / res, h - (n_ - box[1]) / res
            d.ellipse([px - r_, py - r_, px + r_, py + r_], outline=(255, 120, 40) if k_ else (255, 255, 255))
        im.save(a.preview)
    print(f"{out['n']:,} trees: height median {np.median(ht):.1f} m (90% under {np.percentile(ht, 90):.1f} m), "
          f"crown radius median {np.median(rt):.1f} m; {int(kind.sum())} columnar")
    print("wrote", a.out)


if __name__ == "__main__":
    main()
