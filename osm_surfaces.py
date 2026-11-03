"""OpenStreetMap golf features in GSPro's coordinates.

Golf mappers trace fairways, greens, tees, bunkers, rough, water and cart paths as tagged shapes.
This loads an Overpass JSON export (`out geom`), assembles multipolygons, and moves every shape
from latitude/longitude into GSPro's own x/z using the lidar alignment (align_lidar.py), so the
viewer can paint real surface shapes. OSM data is (c) OpenStreetMap contributors, ODbL.
"""
import json
import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

AREA_GOLF = {"fairway", "green", "tee", "bunker", "rough", "water_hazard", "lateral_water_hazard",
             "driving_range", "clubhouse", "out_of_bounds", "practice", "fringe"}


def is_area(t):
    if t.get("area") == "no":
        return False
    if t.get("golf") in AREA_GOLF:
        return True
    return any(k in t for k in ("building", "landuse", "leisure", "water", "amenity")) or \
        t.get("natural") in {"water", "sand", "beach", "wood", "scrub", "grassland", "bare_rock", "wetland"}


def load(paths):
    """[(tags, shapely geometry in lon/lat)] for every tagged element in the Overpass exports."""
    feats, seen = [], set()
    for p in paths:
        for e in json.load(open(p, encoding="utf-8"))["elements"]:
            key = (e["type"], e["id"])
            if key in seen or not e.get("tags"):
                continue
            seen.add(key)
            t = e["tags"]
            if e["type"] == "node":
                g = Point(e["lon"], e["lat"])
            elif e["type"] == "way":
                pts = [(q["lon"], q["lat"]) for q in e.get("geometry") or []]
                if len(pts) < 2:
                    continue
                closed = len(pts) >= 4 and pts[0] == pts[-1]
                g = Polygon(pts) if closed and is_area(t) else LineString(pts)
            elif e["type"] == "relation" and t.get("type") in ("multipolygon", "boundary"):
                ring = lambda role: [LineString([(q["lon"], q["lat"]) for q in m["geometry"]]) for m in e.get("members", [])
                                     if m["type"] == "way" and m.get("role", "outer") == role and len(m.get("geometry") or []) >= 2]
                outer = list(polygonize(unary_union(ring("outer"))))
                if not outer:
                    continue
                g = unary_union(outer)
                inner = list(polygonize(unary_union(ring("inner"))))
                if inner:
                    g = g.difference(unary_union(inner))
            else:
                continue
            if not g.is_valid:
                g = g.buffer(0)
            feats.append((t, g))
    return feats


def to_gspro_fn(align):
    """A function mapping an (N, 2) lon/lat array to GSPro (x, z), via the lidar map grid."""
    from rasterio.crs import CRS
    from rasterio.warp import transform
    crs = CRS.from_string(align["crs"])
    try:   # a compound (horizontal + vertical) CRS: use its horizontal part for 2D points
        epsg = crs.to_epsg() or None
        dst = CRS.from_epsg(epsg) if epsg else crs
    except Exception:
        dst = crs
    o = align["orientation"]
    c, s = np.cos(-align["theta"]), np.sin(-align["theta"])

    def fn(lonlat):
        lonlat = np.asarray(lonlat, dtype=float)
        if not len(lonlat):
            return lonlat
        E, N = transform("EPSG:4326", dst, lonlat[:, 0].tolist(), lonlat[:, 1].tolist())
        dE, dN = (np.array(E) - align["tE"]) / align["scale"], (np.array(N) - align["tN"]) / align["scale"]
        u = align["cu"] + c * dE - s * dN
        v = align["cv"] + s * dE + c * dN
        if o["swap"]:
            x, z = v * o["sx"], u * o["sz"]
        else:
            x, z = u * o["sx"], v * o["sz"]
        return np.stack([x, z], 1)
    return fn


def to_gspro(feats, align, shift=(0.0, 0.0)):
    fn = to_gspro_fn(align)
    dx, dz = shift
    return [(t, shapely.transform(g, lambda a: fn(a) + (dx, dz))) for t, g in feats]


def layers(F):
    """Union of each surface type (GSPro coordinates). Lines (paths, roads) are buffered to their width."""
    polys = lambda pred: shapely.union_all([g for t, g in F if pred(t) and g.geom_type in ("Polygon", "MultiPolygon")] or [Polygon()])
    lines = lambda pred, w: shapely.union_all([g.buffer(w, cap_style="flat") for t, g in F if pred(t) and g.geom_type == "LineString"] or [Polygon()])
    road_w = {"footway": 0.9, "path": 0.9, "track": 1.6, "service": 2.2, "residential": 3.0, "unclassified": 3.0, "tertiary": 3.4, "secondary": 3.6}
    roads = shapely.union_all([g.buffer(road_w.get(t["highway"], 0), cap_style="flat") for t, g in F
                               if t.get("highway") in road_w and g.geom_type == "LineString"] or [Polygon()])
    water_tags = lambda t: t.get("natural") == "water" or "water" in t or t.get("golf") in ("water_hazard", "lateral_water_hazard")
    return {
        "course": polys(lambda t: t.get("leisure") == "golf_course"),
        "rough": polys(lambda t: t.get("golf") == "rough"),
        "fairway": polys(lambda t: t.get("golf") == "fairway"),
        "fairways": [g for t, g in F if t.get("golf") == "fairway" and g.geom_type in ("Polygon", "MultiPolygon")],
        "green": polys(lambda t: t.get("golf") == "green"),
        "tee": polys(lambda t: t.get("golf") == "tee"),
        "bunker": shapely.union_all([polys(lambda t: t.get("golf") == "bunker"), polys(lambda t: t.get("natural") == "sand")]),
        "water": polys(water_tags),
        "path": shapely.union_all([lines(lambda t: t.get("golf") in ("path", "cartpath"), 1.3), polys(lambda t: t.get("golf") in ("path", "cartpath")), roads]),
        "building": polys(lambda t: "building" in t),
        "beach": polys(lambda t: t.get("natural") == "beach"),
    }


def solve_shift(F, checks, span=15):
    """Best (dx, dz) to move OSM onto the course: checks are (points (N,2), layer geometry, weight) where every
    point should sit inside its layer (pins in greens, ball lies in the matching surface). span: metres to search."""
    def score(dx, dz):
        return sum(w * np.mean(np.minimum(shapely.distance(geom, shapely.points(pts - (dx, dz))), 10)) for pts, geom, w in checks if len(pts) and not geom.is_empty)
    grid = np.arange(-span, span + 0.1, 1.0)
    _, bx, bz = min((score(dx, dz), dx, dz) for dx in grid for dz in grid)
    s, bx, bz = min((score(dx, dz), dx, dz) for dx in np.arange(bx - 1.5, bx + 1.51, 0.25) for dz in np.arange(bz - 1.5, bz + 1.51, 0.25))
    return float(bx), float(bz)


def course_checks(rows, gkd, L, pins=None):
    """Points GSPro says are on a green / fairway / bunker, for solve_shift and for reporting. Pins come from
    the course file, or (no course file) from `pins`, the positions worked out from the round."""
    seen, real = set(), []
    for r in rows:   # auto-putt rows repeat the previous start position
        if r["GlobalShotNumber"] not in seen:
            real.append(r)
        seen.add(r["GlobalShotNumber"])
    pts = lambda code: np.array([(r["EndingPOS"]["x"], r["EndingPOS"]["z"]) for r in real if r["EndingSurface"] == code and r["ShotResult"] != 2]
                                + [(r["StartingPOS"]["x"], r["StartingPOS"]["z"]) for r in real if r["StartingSurface"] == code], dtype=float).reshape(-1, 2)
    if gkd:
        pins = np.array([(p["Position"]["x"], p["Position"]["z"]) for h in gkd["Holes"] for p in h.get("Pins") or [] if p.get("Position")], dtype=float).reshape(-1, 2)
    else:
        pins = np.asarray(pins if pins is not None else [], dtype=float).reshape(-1, 2)
    return {"pins": (pins, L["green"], 2.0), "green lies": (pts(5), L["green"], 1.0),
            "fairway lies": (pts(2), L["fairway"], 1.0), "bunker lies": (pts(11), L["bunker"], 1.0)}
