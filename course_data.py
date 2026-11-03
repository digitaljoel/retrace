"""Download everything a course's real terrain needs, once, given its latitude and longitude.

    python course_data.py --folder DIR --lat 40.115 --lon -83.026 --round ROUND.dat [--gkd COURSE.GKD]

Works through these in order and caches each in DIR, so a course only downloads once. Anything that
fails is skipped and tried again next time, and the page is simply built with whatever worked:

  map        OpenStreetMap's fairways, greens, bunkers, water and so on, worldwide
  elevation  public USGS lidar where a survey covers the course (the US and its territories);
             otherwise 5 m worldwide terrain tiles
  alignment  where the course sits on that elevation, checked against where the balls landed. A
             result that doesn't hold up is thrown away, so a page never shows the wrong terrain
  trees      from the lidar point cloud, or else from Meta and WRI's worldwide 1 m canopy height map

The results are listed in DIR/source.json, which retrace.py reads.
"""
import argparse, concurrent.futures as cf, io, json, math, os, re, subprocess, sys, time, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
UA = {"User-Agent": "retrace/0.1 (golf replay viewer)"}
USGS_INDEX = "https://raw.githubusercontent.com/hobuinc/usgs-lidar/master/boundaries/resources.geojson"
TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
HALF_WIDTH, HALF_HEIGHT = 2600, 2000       # metres of terrain tiles around the course point
MAX_SURVEYS = 2                            # surveys to try before moving on to the terrain tiles
LIDAR_CREDIT = "public USGS lidar"
TILES_CREDIT = "Mapzen terrain tiles"
LIDAR_TREES_CREDIT = "the lidar point cloud"
CANOPY_CREDIT = "Meta and WRI's canopy height map (CC BY 4.0)"


def get(url, tries=4):
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return r.read()
        except Exception:
            if k == tries - 1:
                raise
            time.sleep(2 * (k + 1))


def run(script, *args):
    """Run one of the project's scripts, returning True if it worked."""
    done = subprocess.run([sys.executable, str(HERE / script)] + [str(a) for a in args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in done.stdout.strip().splitlines()[-4:]:
        print(f"  {line}", flush=True)
    if done.returncode:
        print(f"  {script} failed: " + " | ".join(done.stderr.strip().splitlines()[-3:]), flush=True)
    return done.returncode == 0


# ---------------------------------------------------------------- elevation
def surveys_at(lat, lon, cache):
    """USGS surveys covering this point, newest first. Only downloads the list for places it could cover."""
    if not (13 <= lat <= 72 and (-180 <= lon <= -60 or 144 <= lon <= 146)):
        return []
    import shapely
    from shapely.geometry import shape
    if not cache.exists() or time.time() - cache.stat().st_mtime > 30 * 86400:
        cache.write_bytes(get(USGS_INDEX))
    found, here = [], shapely.points(lon, lat)
    for f in json.loads(cache.read_text(encoding="utf-8"))["features"]:
        if shape(f["geometry"]).contains(here):
            p = f["properties"]
            years = [int(y) for y in re.findall(r"(?:19|20)\d\d", p["name"])]
            found.append((max(years, default=0), p.get("count", 0), p["url"].rsplit("/ept.json", 1)[0]))
    return [u for _, _, u in sorted(found, reverse=True)]


def tiles_dem(lat, lon, path, z=15, res=5.0):
    """A GeoTIFF of worldwide terrain tiles around the course, in UTM metres."""
    import numpy as np, rasterio
    from PIL import Image
    from pyproj import Transformer
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.warp import reproject
    zone = int((lon + 180) // 6) + 1
    utm = f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}"
    cx, cy = Transformer.from_crs("EPSG:4326", utm, always_xy=True).transform(lon, lat)
    lons, lats = Transformer.from_crs(utm, "EPSG:4326", always_xy=True).transform(
        [cx - HALF_WIDTH, cx + HALF_WIDTH, cx - HALF_WIDTH, cx + HALF_WIDTH], [cy - HALF_HEIGHT, cy - HALF_HEIGHT, cy + HALF_HEIGHT, cy + HALF_HEIGHT])
    n = 2 ** z
    tile_x = lambda lo: int((lo + 180) / 360 * n)
    tile_y = lambda la: int((1 - math.asinh(math.tan(math.radians(la))) / math.pi) / 2 * n)
    x0, x1, y0, y1 = tile_x(min(lons)), tile_x(max(lons)), tile_y(max(lats)), tile_y(min(lats))
    M = np.zeros(((y1 - y0 + 1) * 256, (x1 - x0 + 1) * 256), np.float32)

    def fetch(xy):
        x, y = xy
        a = np.asarray(Image.open(io.BytesIO(get(TILES.format(z=z, x=x, y=y)))).convert("RGB"), np.float32)
        M[(y - y0) * 256:(y - y0 + 1) * 256, (x - x0) * 256:(x - x0 + 1) * 256] = a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(fetch, [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]))
    world = 2 * math.pi * 6378137
    src_tr = from_origin(-world / 2 + x0 * world / n, world / 2 - y0 * world / n, world / n / 256, world / n / 256)
    w, h = int(2 * HALF_WIDTH / res), int(2 * HALF_HEIGHT / res)
    Z = np.zeros((h, w), np.float32)
    tr = from_origin(cx - HALF_WIDTH, cy + HALF_HEIGHT, res, res)
    reproject(M, Z, src_transform=src_tr, src_crs="EPSG:3857", dst_transform=tr, dst_crs=utm, resampling=Resampling.bilinear)
    Z[Z < -6] = -999999.0    # the sea floor: treat it as sea, as lidar does, while keeping low-lying land
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32", crs=utm, transform=tr, nodata=-999999.0, compress="deflate", predictor=3) as d:
        d.write(Z, 1)


def alignment_holds(path):
    """Whether a solved alignment agrees with where the balls landed."""
    al = json.loads(Path(path).read_text(encoding="utf-8"))
    q = al.get("quality") or {}
    ball = (al.get("fit", {}).get("ball") or {}).get("median_abs_m", 99)
    if q.get("greenLies", 0) >= 3:
        verdict = q["greenInside"] >= 0.5
        why = f"{q['greenInside'] * 100:.0f}% of green lies on a mapped green"
    elif "greenLies" in q:
        verdict, why = al["coarseSpread"] <= 3.0, f"height spread {al['coarseSpread']:.2f} m"
    else:
        verdict, why = ball <= 0.5, f"balls within {ball:.2f} m of the ground"
    print(f"  alignment {'accepted' if verdict else 'rejected'}: {why}", flush=True)
    return verdict


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", required=True, type=Path)
    ap.add_argument("--lat", required=True, type=float)
    ap.add_argument("--lon", required=True, type=float)
    ap.add_argument("--round", required=True)
    ap.add_argument("--gkd", default="-")
    ap.add_argument("--no-lidar", action="store_true", help="use the worldwide sources even where lidar exists")
    a = ap.parse_args()
    d = a.folder
    d.mkdir(parents=True, exist_ok=True)
    state_file = d / "source.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}

    def save():
        tmp = d / "source.json.tmp"
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        os.replace(tmp, state_file)

    # the map comes first, because the worldwide alignment uses it
    osm = [d / "osm_golf.json", d / "osm_cover.json"]
    if not state.get("map"):
        print("downloading the OpenStreetMap data", flush=True)
        if run("fetch_osm.py", "--lat", a.lat, "--lon", a.lon, "--out", d / "osm"):
            state["map"] = True
            save()
    have_map = bool(state.get("map"))

    dem, align = d / "elevation.tif", d / "align.json"

    def try_alignment(rigid):
        align.unlink(missing_ok=True)
        args = [a.round, a.gkd, align, dem]
        if rigid:
            args += ["--rigid"] + ([f"--osm={p}" for p in osm] if have_map else [])
        return run("align_lidar.py", *args) and alignment_holds(align)

    if state.get("elevation") and not state.get("aligned"):
        # the elevation is here but no alignment has held up yet: this round may have more to go on
        print("trying the alignment again", flush=True)
        state["aligned"] = try_alignment(state["elevation"] == "tiles")
        save()

    if not state.get("elevation"):
        surveys = []
        if not a.no_lidar:
            try:
                surveys = surveys_at(a.lat, a.lon, d.parent / "usgs_surveys.json")
            except Exception as e:
                print(f"  couldn't read the list of lidar surveys: {e}", flush=True)
        print(f"lidar surveys here: {len(surveys)}", flush=True)
        for url in surveys[:MAX_SURVEYS]:
            print(f"reading lidar ground from {url}", flush=True)
            dem.unlink(missing_ok=True)
            if run("lidar_trees.py", "--make-dem", dem, "--lat", a.lat, "--lon", a.lon, "--ept", url) and try_alignment(False):
                state.update(elevation="lidar", survey=url, aligned=True, credit=LIDAR_CREDIT)
                save()
                break
        if not state.get("elevation"):
            print("downloading worldwide terrain tiles", flush=True)
            try:
                tiles_dem(a.lat, a.lon, dem)
                state.update(elevation="tiles", credit=TILES_CREDIT, aligned=try_alignment(True))
                save()
            except Exception as e:
                print(f"  terrain tiles failed: {e}", flush=True)

    if state.get("aligned") and not state.get("trees"):
        print("finding the trees", flush=True)
        trees = d / "trees.json"
        source = ["--ept", state["survey"]] if state["elevation"] == "lidar" else ["--canopy"]
        extra = sum((["--osm", p] for p in osm), []) if have_map else []
        if run("lidar_trees.py", "--dem", dem, "--align", align, "--out", trees, *source, *extra):
            state["trees"] = LIDAR_TREES_CREDIT if state["elevation"] == "lidar" else CANOPY_CREDIT
            save()
    save()


if __name__ == "__main__":
    main()
