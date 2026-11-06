"""GSPro currentRound.dat (any number of players) + GreenKeeper .GKD course data
-> compact JSON for the 3D round viewer.

The course geometry itself (.gspcrse) is encrypted, so the ground is placeholder
terrain fitted to every ground height we legitimately have: ball positions and
roll paths from the round, plus tee, pin, aim-point, hazard and boundary points
from the GKD. Surfaces are drawn from the GKD layout and the lies GSPro logged.

Coordinates: Unity world (left-handed, metres, Y up) -> three.js (right-handed):
X = x - ox, Y = y, Z = -(z - oz).
"""
import base64, io, json, math, os, re, sys
import numpy as np
from scipy.interpolate import griddata
from scipy.ndimage import gaussian_filter, zoom
from scipy.spatial import cKDTree

ROUND, GKD, IMGDIR, OUT = sys.argv[1:5]
# when the round was played: an optional 5th argument (YYYY-MM-DD, or epoch ms), otherwise the time
# GSPro last wrote currentRound.dat, which is when the final shot of the round was recorded
# optional extras after OUT: a played date, plus --dem=a.tif[,b.tif] and --align=align.json for real terrain
_extra = sys.argv[5:]
_opts = dict(a[2:].split("=", 1) for a in _extra if a.startswith("--") and "=" in a)
_pos = [a for a in _extra if not a.startswith("--")]
PLAYED = _pos[0] if _pos else None
DEM = [d for d in _opts.get("dem", "").split(",") if d] or None
ALIGN = _opts.get("align")
OSM = [d for d in _opts.get("osm", "").split(",") if d] or None   # Overpass JSON exports (out geom)
OSM_SHIFT = _opts.get("osm-shift")
TREES = _opts.get("trees")                                            # real trees from lidar_trees.py
BUILDINGS = _opts.get("buildings")                                    # measured building heights from lidar_trees.py
ALIGN_DATA = None
rows = json.load(open(ROUND, encoding="utf-8-sig"))
gkd = json.load(open(GKD, encoding="utf-8-sig")) if GKD != "-" and os.path.exists(GKD) else None
# some course files have no out-of-bounds line, or no hazards: the field is then missing, null or empty
OOB_COORDS = ((gkd or {}).get("pOOB") or {}).get("coords") or []
HAZARDS = [hz["coords"] for hz in (gkd or {}).get("Hazards") or [] if len(hz.get("coords") or []) >= 3]
YD = 1.0936133
LIE = {18: "Tee", 1: "Rough", 2: "Fairway", 3: "Deep rough", 4: "Cart path", 5: "Green", 11: "Bunker"}
MAT = {"TVGfairway": "Fairway", "TVGrough": "Rough", "TVGdeeprough": "Deep rough",
       "TVGgreen": "Green", "TVGconcrete": "Cart path", "TVGsand": "Bunker"}
STD_TEES = ("Black", "Blue", "Yellow", "White", "Red", "Junior", "Green")
rng = np.random.default_rng(11)


def U(p):
    return np.array([p["x"], p["y"], p["z"]], dtype=float)


pts_all = [U(r[k]) for r in rows for k in ("StartingPOS", "EndingPOS")]
if gkd:
    pts_all += [U(c) for c in OOB_COORDS]
A = np.array(pts_all)
OX, OZ = (A[:, 0].min() + A[:, 0].max()) / 2, (A[:, 2].min() + A[:, 2].max()) / 2


def T(p):
    p = U(p) if isinstance(p, dict) else p
    return np.array([p[0] - OX, p[1], -(p[2] - OZ)])


def xz(v):
    return np.array([v[0], v[2]])


r1 = lambda v: [round(float(a), 2) for a in v]
hole_ids = sorted({r["Hole"] for r in rows})


# ---------------------------------------------------------------- per-player items
def shot_item(r, kind):
    sd, g = r["activeShot"]["sd"], r["GhostData"]
    flight = [T(p) for p in sd["pos"]]
    start, end = T(r["StartingPOS"]), T(r["EndingPOS"])
    apex = (max(p[1] for p in flight) - start[1]) if flight else 0.0
    return {
        "kind": kind, "stroke": r["HoleShot"], "gsn": r["GlobalShotNumber"], "club": r["ClubIndex"],
        "putt": bool(sd["isPutt"]) or not flight,
        "start": start, "end": end, "flight": flight, "flightT": r["activeShot"]["tdTime"] if flight else 0.0,
        "roll": [(T(p["pos"]), p["time"]) for p in sd["path"]],
        "td": T(sd["tdpoint"]) if flight else start,
        "tdLie": MAT.get(g.get("materialHit"), g.get("materialHit")),
        "fromCode": r["StartingSurface"], "toCode": r["EndingSurface"],
        "fromLie": LIE.get(r["StartingSurface"], "?"), "toLie": LIE.get(r["EndingSurface"], "?"),
        "toPin": 0.0 if r["HoleResult"] == 7 else r["DistanceToPin"],   # a holed row gives the distance it started from
        "mph": g["sp"], "vla": g["el"], "hla": g["az"], "spin": g["ts"],
        "axis": g["sa"], "apexFt": apex * 3.28084, "hazardNo": sd["HazardNumber"],
        "holed": r["HoleResult"] == 7,
    }


def auto_putt(r):
    """GSPro's auto-putts: the ball doesn't move, and the row repeats the shot data of the stroke before it.
    (Older versions gave them that stroke's shot number instead, which build_items also catches.)"""
    b = ((r.get("activeShot") or {}).get("sd") or {}).get("BallStartPos")
    s, e = r["StartingPOS"], r.get("EndingPOS")
    return bool(b and e) and abs(b["x"] - s["x"]) + abs(b["z"] - s["z"]) > 0.05 and abs(e["x"] - s["x"]) + abs(e["z"] - s["z"]) < 0.05


def build_items(hr):
    items, counted = [], set()
    for r in hr:
        gsn = r["GlobalShotNumber"]
        if r["ShotResult"] in (2, 3):
            # 2: penalty area, the row ends at the drop; 3: out of bounds / lost ball, stroke and distance:
            # the row ends back where the shot was played from
            items.append({"kind": "penalty", "stroke": r["HoleShot"], "gsn": gsn, "drop": T(r["EndingPOS"]),
                          "dropCode": r["EndingSurface"], "dropLie": LIE.get(r["EndingSurface"], "?"),
                          "toPin": r["DistanceToPin"], "ob": r["ShotResult"] == 3})
        elif r["ShotResult"] == 1:
            items.append(shot_item(r, "mulligan"))  # replayed: same stroke number, not counted
        elif gsn in counted or auto_putt(r):
            counted.add(gsn)
            items.append({"kind": "putt", "stroke": r["HoleShot"], "gsn": gsn, "holed": r["HoleResult"] == 7})
            if r["HoleResult"] == 7 and auto_putt(r):
                # the holing row's distance is the first putt's length; the putts in between aren't recorded
                first = len(items) - 1
                while first > 0 and items[first - 1]["kind"] == "putt":
                    first -= 1
                items[first]["fromM"] = r["TotalDistance"]
        else:
            counted.add(gsn)
            items.append(shot_item(r, "shot"))
    for k, it in enumerate(items):
        if it["kind"] in ("shot", "mulligan"):
            it["pen"] = it["kind"] == "shot" and k + 1 < len(items) and items[k + 1]["kind"] == "penalty"
            it["ob"] = it["pen"] and items[k + 1].get("ob", False)
    return items


uids = []
for r in rows:
    if r["UserGuid"] not in uids:
        uids.append(r["UserGuid"])
players = []
for uid in uids:
    prow = [r for r in rows if r["UserGuid"] == uid]
    ph = []
    for hidx in hole_ids:
        hr = [r for r in prow if r["Hole"] == hidx]
        if not hr:
            ph.append(None)
            continue
        ph.append({"strokes": max(r["HoleShot"] for r in hr), "complete": hr[-1]["HoleResult"] == 7, "items": build_items(hr)})
    players.append({"name": prow[0]["PlayerName"], "id": uid, "holes": ph})


# ---------------------------------------------------------------- course layout (GKD + round)
def pin_pairs(hidx):
    """(rest xz, distance-to-pin) from every counted ball position on the hole, all players"""
    out = []
    for p in players:
        h = p["holes"][hole_ids.index(hidx)]
        if not h:
            continue
        for it in h["items"]:
            if it["kind"] == "shot" and not it.get("pen"):
                out.append((xz(it["end"]), it["toPin"]))
            elif it["kind"] == "penalty":
                out.append((xz(it["drop"]), it["toPin"]))
    return out


def lsq_pin(pairs, guess):
    """The pin from (ball position, distance to pin) pairs. Robust start: the ball that finished closest
    puts the pin on a small circle; take the point of that circle that best agrees with the others, then
    refine on the readings that agree (a stray reading can't drag it away)."""
    E = np.array([p[0] for p in pairs]); rr = np.array([p[1] for p in pairs])
    k = int(np.argmin(rr)); c, r0 = E[k], rr[k]
    if len(E) >= 2:
        ang = np.linspace(0, 2 * np.pi, 1440, endpoint=False)
        cand = c + r0 * np.stack([np.cos(ang), np.sin(ang)], 1)
        others = np.arange(len(E)) != k
        err = np.median(np.abs(np.linalg.norm(cand[:, None, :] - E[None, others, :], axis=2) - rr[None, others]), axis=1)
        x0 = cand[int(np.argmin(err))]
        # with only two readings the circles cross twice: keep every point that fits as well, for later
        best = err.min(); lm = (err <= np.roll(err, 1)) & (err <= np.roll(err, -1)) & (err < best + 1.0)
        lsq_pin.alts = [cand[i] for i in np.flatnonzero(lm) if np.linalg.norm(cand[i] - x0) > 3]
    else:
        # one reading gives the distance but not the direction: stay at that distance, facing the guess,
        # and keep the rest of the circle so a mapped green can choose a better spot later
        d = guess - c
        x0 = c + r0 * d / (np.linalg.norm(d) + 1e-9)
        ang = np.linspace(0, 2 * np.pi, 72, endpoint=False)
        lsq_pin.alts = list(c + r0 * np.stack([np.cos(ang), np.sin(ang)], 1))
    inl =np.abs(np.linalg.norm(x0 - E, axis=1) - rr) < max(4.0, 0.03 * rr.min())
    inl[k] = True
    x = x0.copy()
    for _ in range(100):
        diff = x - E[inl]; dist = np.linalg.norm(diff, axis=1) + 1e-9
        J = diff / dist[:, None]; dx, *_ = np.linalg.lstsq(J, -(dist - rr[inl]), rcond=None)
        dx = dx * min(1.0, 2.0 / (np.linalg.norm(dx) + 1e-12))      # small steps only
        x = x + dx
        if np.linalg.norm(dx) < 1e-7:
            break
    if np.linalg.norm(x - x0) > 3 or not np.all(np.isfinite(x)):  # refinement wandered: keep the circle point
        x = x0
    return x, float(np.median(np.abs(np.linalg.norm(x - E[inl], axis=1) - rr[inl])))


course_holes, tee_types, pin_days = [], [], []
for hidx in hole_ids:
    hrows = [r for r in rows if r["Hole"] == hidx]
    first = [T(r["StartingPOS"]) for r in hrows if r["HoleShot"] == 1 and r["ShotResult"] != 2]
    pairs = pin_pairs(hidx)
    entry = {"n": hidx + 1, "par": hrows[0]["HolePar"], "index": None, "aims": [], "pins": [], "teeBoxes": []}
    gh = gkd["Holes"][hidx] if gkd and hidx < len(gkd["Holes"]) else None
    if gh:
        entry["index"] = gh["Index"]
        tees = [t for t in gh["Tees"] if t["Enabled"] and t["TeeType"] in STD_TEES and (t["Position"]["x"] or t["Position"]["z"])]
        played = min(tees, key=lambda t: np.linalg.norm(xz(T(t["Position"])) - xz(first[0])))
        dt = np.linalg.norm(xz(T(played["Position"])) - xz(first[0]))
        entry["tee"] = T(played["Position"]) if dt < 4 else first[0]
        entry["teeType"] = played["TeeType"] if dt < 4 else None
        entry["yards"] = played["Distance"] * YD if dt < 4 else None
        entry["teeBoxes"] = [T(t["Position"]) for t in tees]
        entry["aims"] = [T(t["Position"]) for t in gh["Tees"] if t["TeeType"].startswith("AimPoint") and t["Enabled"] and (t["Position"]["x"] or t["Position"]["z"])]
        entry["pins"] = [T(p["Position"]) for p in gh["Pins"]]
        best = None
        E = np.array([p[0] for p in pairs]); rr = np.array([p[1] for p in pairs])
        for p in gh["Pins"]:
            P = xz(T(p["Position"]))
            res = float(np.abs(np.linalg.norm(E - P, axis=1) - rr).max())
            if best is None or res < best[1]:
                best = (p, res)
        if best[1] < 3.0:
            entry["pin"] = T(best[0]["Position"]); entry["pinDay"] = best[0]["Day"]; entry["pinResid"] = best[1]
        tee_types.append(entry["teeType"]); pin_days.append(entry.get("pinDay"))
    else:
        entry["tee"] = first[0]
    if "pin" not in entry:
        x, res = lsq_pin(pairs, xz(entry["tee"]))
        y = max(it["end"][1] for p in players for it in (p["holes"][hole_ids.index(hidx)] or {"items": []})["items"] if it["kind"] == "shot")
        entry["pin"] = np.array([x[0], y, x[1]]); entry["pinResid"] = res; entry["pinDay"] = None
        entry["pinAlts"] = [np.array([q[0], y, q[1]]) for q in getattr(lsq_pin, "alts", [])]
        entry["pinOneReading"] = len(pairs) == 1
    if not entry.get("yards"):
        entry["yards"] = float(np.linalg.norm(xz(entry["tee"]) - xz(entry["pin"])) * YD)
    if not entry["pins"]:
        entry["pins"] = [entry["pin"]]
    course_holes.append(entry)
    print(f"hole {entry['n']:>2} par {entry['par']} {entry['yards']:.0f} yd tee {entry.get('teeType')} pin {entry.get('pinDay')} resid {entry['pinResid']:.2f} m aims {len(entry['aims'])}")

# ---------------------------------------------------------------- terrain samples
samples, flight_cons, track_xz, roll_xz, rest_pts = [], [], [], [], []
for p in players:
    for hi, h in enumerate(p["holes"]):
        if not h:
            continue
        for it in h["items"]:
            if it["kind"] in ("shot", "mulligan"):
                for q in (it["start"], it["end"], it["td"]):
                    samples.append((q[0], q[2], q[1]))
                roll = it["roll"]
                for k, (q, t) in enumerate(roll):
                    roll_xz.append(xz(q)); track_xz.append(xz(q))
                    if k >= len(roll) * 0.45:
                        samples.append((q[0], q[2], q[1] - 0.05))
                fl = it["flight"]
                if fl:
                    # only points that are genuinely airborne constrain the ground: height above the
                    # straight line from launch to touchdown must exceed 2.5 m (chips and the ends of
                    # approaches fly low and would otherwise dig hollows around greens)
                    a, b = it["start"], it["td"]
                    L = max(1e-6, np.hypot(b[0] - a[0], b[2] - a[2]))
                for k, q in enumerate(fl):
                    track_xz.append(xz(q))
                    s_ = min(1.0, np.hypot(q[0] - a[0], q[2] - a[2]) / L)
                    if q[1] - (a[1] + (b[1] - a[1]) * s_) > 2.5:
                        flight_cons.append((q[0], q[2], q[1]))
                if it["kind"] == "shot":
                    rest_pts.append((xz(it["start"]), it["fromCode"], hi))
                    if not it["pen"]:
                        rest_pts.append((xz(it["end"]), it["toCode"], hi))
            elif it["kind"] == "penalty":
                q = it["drop"]; samples.append((q[0], q[2], q[1]))
                rest_pts.append((xz(q), it["dropCode"], hi))
hazards, oob = [], None
n_core = len(samples)
if gkd:
    for ch in course_holes:
        for q in ch["teeBoxes"] + ch["pins"] + ch["aims"]:
            samples.append((q[0], q[2], q[1]))
    n_core = len(samples)
    for coords in HAZARDS:
        poly = np.array([T(c) for c in coords])
        hazards.append(poly)
        for q in poly:
            samples.append((q[0], q[2], q[1]))
    if len(OOB_COORDS) >= 3:
        oob = np.array([T(c) for c in OOB_COORDS])
        for q in oob:
            samples.append((q[0], q[2], q[1]))
def dedup(a):
    a = np.array(a); key = np.round(a[:, :2] / 2.0).astype(int)
    _, inv = np.unique(key, axis=0, return_inverse=True); inv = inv.ravel(); cnt = np.bincount(inv)
    return np.stack([np.bincount(inv, a[:, i]) / cnt for i in range(3)], axis=1)


S = dedup(samples)
S_core = dedup(samples[:n_core]) if gkd else S
track_xz = np.array(track_xz); roll_xz = np.array(roll_xz)

CELL, PAD = (4.0, 150.0) if DEM else (6.0, 130.0)
x0, x1 = S[:, 0].min() - PAD, S[:, 0].max() + PAD
z0, z1 = S[:, 1].min() - PAD, S[:, 1].max() + PAD
nx = int(math.ceil((x1 - x0) / CELL)) + 1
nz = int(math.ceil((z1 - z0) / CELL)) + 1
x1, z1 = x0 + (nx - 1) * CELL, z0 + (nz - 1) * CELL
GX, GZ = np.meshgrid(x0 + np.arange(nx) * CELL, z0 + np.arange(nz) * CELL)
pts = np.stack([GX.ravel(), GZ.ravel()], axis=1)
D = cKDTree(S[:, :2]).query(pts)[0].reshape(nz, nx)
lin = griddata(S[:, :2], S[:, 2], pts, method="linear").reshape(nz, nx)
near = griddata(S[:, :2], S[:, 2], pts, method="nearest").reshape(nz, nx)
H = np.where(np.isnan(lin), near, lin)
Aq = np.stack([np.ones(len(S)), S[:, 0], S[:, 1], S[:, 0] ** 2, S[:, 1] ** 2, S[:, 0] * S[:, 1]], 1)
coef, *_ = np.linalg.lstsq(Aq, S[:, 2], rcond=None)
BG = coef[0] + coef[1] * GX + coef[2] * GZ + coef[3] * GX ** 2 + coef[4] * GZ ** 2 + coef[5] * GX * GZ


def lidar_ground(GX, GZ):
    """Real ground from lidar: GSPro coordinates -> map grid via the solved alignment, cell-averaged."""
    import rasterio
    from rasterio.windows import from_bounds
    from scipy.ndimage import map_coordinates
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import align_lidar as AL
    global ALIGN_DATA
    if ALIGN and os.path.exists(ALIGN):
        al = json.load(open(ALIGN))
    else:   # solve it now and keep it for next time
        tmp = ALIGN or os.path.join(os.path.dirname(OUT) or ".", "align.json")
        sys.argv = ["align_lidar.py", ROUND, GKD, tmp] + DEM; AL.main(); al = json.load(open(tmp))
    ALIGN_DATA = al
    o = al["orientation"]
    ux, uz = GX + OX, OZ - GZ                                     # back to GSPro's own x, z
    u, v = AL.orient(ux, uz, (o["swap"], o["sx"], o["sz"]))
    c, s_ = np.cos(al["theta"]), np.sin(al["theta"])
    E = al["tE"] + al["scale"] * (c * (u - al["cu"]) - s_ * (v - al["cv"]))
    N = al["tN"] + al["scale"] * (s_ * (u - al["cu"]) + c * (v - al["cv"]))
    acc = np.zeros(E.shape); wsum = np.zeros(E.shape)
    for path in DEM:
        with rasterio.open(path) as src:
            m = 4 * CELL
            win = from_bounds(E.min() - m, N.min() - m, E.max() + m, N.max() + m, src.transform).round_offsets().round_lengths()
            Z = src.read(1, window=win, boundless=True, fill_value=np.nan).astype(np.float64)
            if src.nodata is not None:
                Z[Z == src.nodata] = np.nan
            Z[Z < -1000] = np.nan
            tr = src.window_transform(win)
        ok = np.isfinite(Z)
        # average over a cell's footprint (normalised so the coast doesn't drag toward no-data)
        sig = CELL / (2.4 * abs(tr.a))
        num = gaussian_filter(np.where(ok, Z, 0), sig); den = gaussian_filter(ok.astype(float), sig)
        col = (E - tr.c) / tr.a - 0.5; row = (N - tr.f) / tr.e - 0.5
        nv = map_coordinates(num, [row, col], order=1, mode="constant", cval=0)
        dv = map_coordinates(den, [row, col], order=1, mode="constant", cval=0)
        inside = (col >= 0) & (row >= 0) & (col <= Z.shape[1] - 1) & (row <= Z.shape[0] - 1)
        acc += np.where(inside, nv, 0); wsum += np.where(inside, dv, 0)
    land = wsum > 0.5
    elev = np.where(land, acc / np.maximum(wsum, 1e-9), np.nan)
    sea = al["hOffset"]                                           # lidar 0 m (sea level) in GSPro heights
    # a gap in the lidar is the sea only on a coast; inland it's the survey's edge or a lake with no ground
    # returns, so it takes the height of the nearest land rather than dropping to sea level
    if (~land).any() and land.any() and np.nanmin(elev) > 30:
        from scipy.ndimage import distance_transform_edt
        elev = elev[tuple(distance_transform_edt(~land, return_distances=False, return_indices=True))]
        print(f"lidar gaps inland ({(~land).mean() * 100:.1f}% of cells): filled from the nearest ground")
        land = np.ones_like(land)
    Hl = np.where(land, al["hScale"] * elev + al["hOffset"], sea - 4.0)
    print(f"lidar ground: {land.mean() * 100:.0f}% land, heights {np.nanmin(Hl):.1f} to {np.nanmax(Hl):.1f} m; "
          f"alignment fit {al['fit'].get('ball', {}).get('median_abs_m', '?')} m median on ball rests")
    return Hl, (sea if (~land).any() else None)


def smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def vnoise(scale):
    c = rng.normal(size=(nz // scale + 3, nx // scale + 3))
    return zoom(c, (nz / c.shape[0], nx / c.shape[1]), order=3)[:nz, :nx]


SEA = None
if DEM:
    H, SEA = lidar_ground(GX, GZ)
    S, D = S_core, cKDTree(S_core[:, :2]).query(pts)[0].reshape(nz, nx)   # snap only to exact points
else:
    H = (1 - smoothstep(25, 140, D)) * H + smoothstep(25, 140, D) * BG
    H = H + smoothstep(30, 200, D) * (vnoise(10) * 7 + vnoise(3) * 1.6)
    edge = np.minimum.reduce([GX - x0, x1 - GX, GZ - z0, z1 - GZ])
    H = H + smoothstep(0, 1, (110 - edge) / 110) * 16
    H = gaussian_filter(H, 1.0)


def tri_sample(Hg, X, Z):
    fx = (X - x0) / CELL; fz = (Z - z0) / CELL
    i = np.clip(np.floor(fx).astype(int), 0, nx - 2); j = np.clip(np.floor(fz).astype(int), 0, nz - 2)
    u = fx - i; v = fz - j
    h00 = Hg[j, i]; h10 = Hg[j, i + 1]; h01 = Hg[j + 1, i]; h11 = Hg[j + 1, i + 1]
    return np.where(u + v <= 1, h00 + u * (h10 - h00) + v * (h01 - h00), h11 + (1 - u) * (h01 - h11) + (1 - v) * (h10 - h11))


def fit_to_samples(H, iters, fade):
    # pull the surface onto every recorded ground point, fading the correction out with distance
    for _ in range(iters):
        res = S[:, 2] - tri_sample(H, S[:, 0], S[:, 1])
        rl = griddata(S[:, :2], res, pts, method="linear").reshape(nz, nx)
        rn = griddata(S[:, :2], res, pts, method="nearest").reshape(nz, nx)
        H = H + np.where(np.isnan(rl), rn, rl) * (1 - smoothstep(fade[0], fade[1], D))
    return H


H = fit_to_samples(H, 3, (2.5, 18)) if DEM else fit_to_samples(H, 4, (8, 70))
FC = np.array(flight_cons) if flight_cons else np.zeros((0, 3))
for _ in range(4):
    # keep the ground at least 1 m under airborne flight, touching only the cells right under it,
    # then re-fit to the ball positions so greens and fairways stay where the balls are
    if not len(FC):
        break
    # real ground is trusted: only fix flights that would pass through it (a ball can skim a real ridge)
    clear_ = FC[:, 2] - tri_sample(H, FC[:, 0], FC[:, 1]); bad = clear_ < (0.25 if DEM else 1.0)
    if _ == 0:
        print(f"airborne points {len(FC)}: clearance under 1 m {int((clear_ < 1).sum())}, under 0.25 m {int((clear_ < 0.25).sum())}, min {clear_.min():.2f} m")
    if not bad.any():
        break
    for (fx_, fz_, fy_) in FC[bad]:
        i = int(round((fx_ - x0) / CELL)); j = int(round((fz_ - z0) / CELL))
        sl = (slice(max(j - 1, 0), j + 2), slice(max(i - 1, 0), i + 2))
        H[sl] = np.minimum(H[sl], fy_ - (0.4 if DEM else 1.2))
    H = fit_to_samples(H, 2, (2.5, 18) if DEM else (4, 40))
RP = np.array([(q[0], q[1]) for q, c, k in rest_pts])
rest_y = []
for p in players:
    for h in p["holes"]:
        for it in (h or {"items": []})["items"]:
            if it["kind"] == "shot":
                rest_y.append((it["start"][0], it["start"][2], it["start"][1]))
RY = np.array(rest_y)
rres = RY[:, 2] - tri_sample(H, RY[:, 0], RY[:, 1])
print(f"grid {nx}x{nz} @ {CELL} m; rest-point fit median {np.median(np.abs(rres)):.2f} m, max {np.abs(rres).max():.2f} m; "
      f"airborne clearance min {np.min(FC[:, 2] - tri_sample(H, FC[:, 0], FC[:, 1])) if len(FC) else 0:.2f} m")

# ---------------------------------------------------------------- surfaces
# 0 natural, 1 rough, 2 fairway, 3 green, 4 tee, 5 bunker, 6 penalty area, 7 cart path, 8 fringe, 9 deep rough
TAx, TAz = np.meshgrid(x0 + (np.arange(nx - 1) + 1 / 3) * CELL, z0 + (np.arange(nz - 1) + 1 / 3) * CELL)
TBx, TBz = np.meshgrid(x0 + (np.arange(nx - 1) + 2 / 3) * CELL, z0 + (np.arange(nz - 1) + 2 / 3) * CELL)
P = np.stack([np.stack([TAx, TAz], -1), np.stack([TBx, TBz], -1)], axis=2).reshape(-1, 2)
CLS = np.zeros(len(P), dtype=np.uint8)


def seg_dist(Q, a, b):
    ab = b - a; t = np.clip(((Q - a) @ ab) / (ab @ ab + 1e-9), 0, 1)
    return np.linalg.norm(Q - (a + t[:, None] * ab), axis=1), t


def poly_dist(Q, poly):
    best = np.full(len(Q), np.inf); s_at = np.zeros(len(Q)); acc = 0.0
    for a, b in zip(poly[:-1], poly[1:]):
        d, t = seg_dist(Q, a, b); L = np.linalg.norm(b - a); m = d < best
        best[m] = d[m]; s_at[m] = acc + t[m] * L; acc += L
    return best, s_at, acc


def inside_poly(Q, poly):
    x, y = Q[:, 0], Q[:, 1]; px, py = poly[:, 0], poly[:, 1]; n = len(poly); ins = np.zeros(len(Q), bool)
    j = n - 1
    for i in range(n):
        c = ((py[i] > y) != (py[j] > y)) & (x < (px[j] - px[i]) * (y - py[i]) / (py[j] - py[i] + 1e-12) + px[i])
        ins ^= c; j = i
    return ins


near_tree = cKDTree(P)
bunkers = []
for hi, ch in enumerate(course_holes):
    pin = xz(ch["pin"]); tee = xz(ch["tee"])
    route = np.array([tee] + [xz(a) for a in ch["aims"]] + [pin])
    ch["route"] = route
    hole_pts = [(q, c) for (q, c, k) in rest_pts if k == hi]
    pins2 = [xz(p) for p in ch["pins"]]
    # local window for speed
    lo = route.min(0) - 90; hi_ = route.max(0) + 90
    W_ = np.where((P[:, 0] > lo[0]) & (P[:, 0] < hi_[0]) & (P[:, 1] > lo[1]) & (P[:, 1] < hi_[1]))[0]
    Q = P[W_]
    dG = np.min([np.linalg.norm(Q - p, axis=1) for p in pins2], axis=0)
    for a in range(len(pins2)):
        for b in range(a + 1, len(pins2)):
            dG = np.minimum(dG, seg_dist(Q, pins2[a], pins2[b])[0])
    gc = np.mean(pins2, axis=0)
    ang = np.arctan2(Q[:, 1] - gc[1], Q[:, 0] - gc[0])
    wob = 0.9 * np.sin(3 * ang + hi) + 0.6 * np.sin(5 * ang + 2 * hi)
    near_off = [np.min([np.linalg.norm(q - p) for p in pins2]) for q, c in hole_pts if c not in (5,)]
    rg = 8.5
    off_close = [d for d in near_off if d < 14]
    if off_close:
        rg = max(4.5, min(rg, min(off_close) - 2.6))
    d_route, s_route, L = poly_dist(Q, route)
    others = [(q, c) for q, c in hole_pts if c in (1, 3, 4, 11)]
    Wd = np.full(len(Q), 19.0); Rr = np.full(len(Q), 38.0)
    if others:
        o_d, o_s, _ = poly_dist(np.array([q for q, c in others]), route)
        for (q, c), dd, ss in zip(others, o_d, o_s):
            Wd = np.minimum(Wd, dd - 2.5 + np.maximum(0, np.abs(s_route - ss) - 10) * 0.6)
            if c in (1, 11):
                Rr = np.maximum(Rr, (dd + 9) - np.maximum(0, np.abs(s_route - ss) - 15) * 0.8)
    Wd = np.maximum(Wd, 8.0)
    par = ch["par"]
    fw_start = 0 if par == 3 else min(100.0, max(35.0, L * 0.28))
    fw_end = L - 12
    corridor = (d_route < Rr) | (dG < rg + 24) | (np.linalg.norm(Q - tee, axis=1) < 26)
    deep = (d_route < Rr + 14) & ~corridor
    fair = (d_route < Wd) & (s_route > fw_start) & (s_route < fw_end) & (par > 3)
    approach = (par == 3) & (d_route < 9) & (s_route > L - 40) & (s_route < L - 8)
    green = dG < rg + wob
    fringe = (dG < rg + wob + 2.4) & ~green
    c = CLS[W_]
    c[deep & (c == 0)] = 9
    c[corridor & np.isin(c, (0, 9))] = 1
    c[(fair | approach) & np.isin(c, (0, 9, 1))] = 2
    c[fringe & np.isin(c, (0, 9, 1, 2))] = 8
    c[green] = 3
    # every tee box on the hole, oriented down the first leg
    dirv = route[1] - route[0]; dirv /= np.linalg.norm(dirv); perp = np.array([-dirv[1], dirv[0]])
    for tb in ch["teeBoxes"] + [ch["tee"]]:
        rel = Q - xz(tb); al = rel @ dirv; sd_ = rel @ perp
        c[(al > -9) & (al < 5) & (np.abs(sd_) < 5)] = 4
    CLS[W_] = c
    ch["greenR"] = rg
    back = route[-2] - pin; back /= np.linalg.norm(back)
    for sgn, deg in ((1, 64 + 9 * (hi % 3)), (-1, 76 - 7 * (hi % 2))):
        a = math.radians(deg) * sgn
        rot = np.array([back[0] * math.cos(a) - back[1] * math.sin(a), back[0] * math.sin(a) + back[1] * math.cos(a)])
        far = max(np.dot(p - pin, rot) for p in pins2)
        bunkers.append((pin + rot * (max(far, 0) + rg + 7), np.array([-rot[1], rot[0]]), 8.0, 4.2, False))

# real bunkers where a ball lay in sand
for q, c, k in rest_pts:
    if c == 11:
        bunkers.append((q, np.array([1.0, 0.0]), 7.5, 6.0, True))
blockers = np.array([q for q, c, k in rest_pts if c != 11])
for cen, axis, a, b, real in bunkers:
    def inside(Q, cen=cen, axis=axis, a=a, b=b):
        rel = Q - cen; u = rel @ axis; v = rel @ np.array([-axis[1], axis[0]])
        return (u / a) ** 2 + (v / b) ** 2 < 1
    if not real and (inside(blockers).any() or inside(roll_xz).any()):
        continue
    idx = near_tree.query_ball_point(cen, max(a, b) + 1)
    if not idx:          # a bunker that falls off the edge of the ground grid
        continue
    m = inside(P[idx])
    sel = np.array(idx, dtype=int)[m]
    CLS[sel[np.isin(CLS[sel], (0, 1, 2, 8, 9))]] = 5

# real penalty areas and boundary from the GKD
stakes_red, stakes_white = [], []
for poly in hazards:
    p2 = np.array([xz(q) for q in poly])
    lo = p2.min(0) - 2; hi_ = p2.max(0) + 2
    W_ = np.where((P[:, 0] > lo[0]) & (P[:, 0] < hi_[0]) & (P[:, 1] > lo[1]) & (P[:, 1] < hi_[1]))[0]
    m = inside_poly(P[W_], p2)
    sel = W_[m]
    CLS[sel[~np.isin(CLS[sel], (3, 4))]] = 6
    ring = np.vstack([p2, p2[:1]])
    for a, b in zip(ring[:-1], ring[1:]):
        n = max(1, int(np.linalg.norm(b - a) // 9))
        for t in range(n):
            stakes_red.append(a + (b - a) * t / n)
if oob is not None:
    o2 = np.array([xz(q) for q in oob])
    outside = ~inside_poly(P, o2)
    CLS[outside & ~np.isin(CLS, (3, 4))] = 0
    ring = np.vstack([o2, o2[:1]])
    for a, b in zip(ring[:-1], ring[1:]):
        n = max(1, int(np.linalg.norm(b - a) // 24))
        for t in range(n):
            stakes_white.append(a + (b - a) * t / n)

# cart path through a concrete lie
for hi, ch in enumerate(course_holes):
    for q, c, k in rest_pts:
        if k == hi and c == 4:
            route = ch["route"]
            seg = int(np.argmin([seg_dist(q[None], a, b)[0][0] for a, b in zip(route[:-1], route[1:])]))
            dv = route[seg + 1] - route[seg]; dv /= np.linalg.norm(dv)
            dd, _ = seg_dist(P, q - dv * 140, q + dv * 140)
            CLS[(dd < 1.8) & ~np.isin(CLS, (3, 4))] = 7

code2cls = {1: 1, 2: 2, 3: 9, 4: 7, 5: 3, 18: 4, 11: 5}
mism = 0
for q, c, k in rest_pts:
    want = code2cls.get(c)
    if want is None:
        continue
    idx = near_tree.query_ball_point(q, 8.0)
    if not idx:
        continue
    idx = np.array(idx); d = np.linalg.norm(P[idx] - q, axis=1)
    here = CLS[idx[np.argmin(d)]]
    if here != want and not (want == 2 and here == 8) and not (want == 3 and here == 8):
        mism += 1
        r = {1: 4.5, 2: 5.0, 9: 7.0, 7: 2.0, 3: 3.0, 4: 3.0, 5: 5.0}[want]
        m = ((d < r) & ~np.isin(CLS[idx], (3, 4))) | (d < 1.5)
        CLS[idx[m]] = want
print("lie mismatches painted:", mism)

# ---------------------------------------------------------------- OpenStreetMap surfaces
# Golf mappers trace fairways, greens, tees, bunkers, water and paths. Where OSM covers a hole, its shapes
# replace the approximations above; they're painted onto the triangles here (so trees keep off them) and
# drawn crisply into a surface texture at pack time. OSM data (c) OpenStreetMap contributors, ODbL.
OSM_L, osm_owned, keep_old, OSM_NAME = None, np.zeros(len(P), bool), None, None
if OSM and ALIGN_DATA is None:
    print("OSM surfaces need the lidar alignment (--dem); skipping them")
elif OSM:
    import shapely, shapely.ops
    from shapely.geometry import LineString as _LS, Polygon as _Poly
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import osm_surfaces as OSF
    F = OSF.to_gspro(OSF.load(OSM), ALIGN_DATA)
    L0 = OSF.layers(F)
    # without a course file, the pins are the ones worked out from the round (viewer X/Z back to GSPro x/z)
    solved_pins = [(ch["pin"][0] + OX, OZ - ch["pin"][2]) for ch in course_holes]
    checks = OSF.course_checks(rows, gkd, L0, solved_pins)
    if OSM_SHIFT and os.path.exists(OSM_SHIFT):
        shift = json.load(open(OSM_SHIFT))["shift"]
    else:   # mapped imagery is often a few metres off; find the shift that puts pins and lies on their surfaces
        shift = OSF.solve_shift(F, list(checks.values()))
        if OSM_SHIFT:
            json.dump({"shift": shift}, open(OSM_SHIFT, "w"))
    print(f"OSM shift {shift[0]:+.2f}, {shift[1]:+.2f} m: " + ", ".join(
        f"{k} {np.mean(shapely.distance(g_, shapely.points(pts - shift)) == 0) * 100:.0f}% inside" for k, (pts, g_, w) in checks.items() if len(pts)))
    to_view = lambda a: np.stack([a[:, 0] + shift[0] - OX, -(a[:, 1] + shift[1] - OZ)], 1)   # GSPro x,z -> viewer X,Z
    OSM_L = {k: ([shapely.transform(g_, to_view) for g_ in v] if isinstance(v, list) else shapely.transform(v, to_view)) for k, v in L0.items()}
    # a pin worked out from only two readings can land on the wrong of two points; if the other is on a green, take it
    # a pin from one reading could be anywhere at that distance from the ball: take the spot deepest inside the green
    for ch in course_holes:
        one = ch.get("pinOneReading")
        if not gkd and ch.get("pinAlts") and (one or not shapely.contains_xy(OSM_L["green"], *xz(ch["pin"]))):
            on = [q for q in ch["pinAlts"] if shapely.contains_xy(OSM_L["green"], *xz(q))]
            if on:
                if one:
                    edge = shapely.boundary(OSM_L["green"])
                    on.sort(key=lambda q: -shapely.distance(edge, shapely.points(*xz(q))))
                ch["pin"] = on[0]; ch["pins"] = [on[0]]
                ch["yards"] = float(np.linalg.norm(xz(ch["tee"]) - xz(ch["pin"])) * YD)
                print(f"hole {ch['n']}: pin placed on a mapped green" + (" from a single reading" if one else ", using the other fit"))
    # a pin a few metres off the mapped green: the green was mapped a little small there (or has grown since). GSPro
    # is the truth, so stretch the green out to take in the pin and the balls GSPro says finished on that green
    from shapely.geometry import MultiPoint as _MP
    for hi, ch in enumerate(course_holes):
        pin = shapely.points(*xz(ch["pin"]))
        gap = float(shapely.distance(OSM_L["green"], pin))
        if 0 < gap <= 8.0:
            on_green = [(r["EndingPOS"]["x"] - OX, -(r["EndingPOS"]["z"] - OZ)) for r in rows
                        if r["Hole"] == ch["n"] - 1 and r.get("EndingSurface") == 5 and r.get("EndingPOS")]
            near = shapely.ops.nearest_points(OSM_L["green"], pin)[0]
            patch = _MP([pin, near] + [shapely.points(*q) for q in on_green]).convex_hull.buffer(2.5)
            OSM_L["green"] = shapely.union(OSM_L["green"], patch)
            print(f"hole {ch['n']}: pin {gap:.1f} m off the mapped green; stretched the green to take it in")
    covered = [any(shapely.contains_xy(OSM_L["green"], *xz(pp)) for pp in ch["pins"]) for ch in course_holes]
    print(f"OSM covers {sum(covered)} of {len(covered)} holes" + ("" if all(covered) else f"; keeping the approximations on holes {[i + 1 for i, c in enumerate(covered) if not c]}"))
    keep_old = shapely.union_all([_LS(ch["route"]).buffer(90) for ch, c in zip(course_holes, covered) if not c] or [_Poly()])
    if not gkd:   # no course file: take the course's name from the map, the mapped course that holds the most pins
        pins_now = shapely.points(np.array([(ch["pin"][0] + OX, OZ - ch["pin"][2]) for ch in course_holes]) - shift)
        named = [(int(shapely.contains(g_, pins_now).sum()), t["name"]) for t, g_ in F
                 if t.get("leisure") == "golf_course" and t.get("name") and g_.geom_type in ("Polygon", "MultiPolygon")]
        if named and max(named)[0] >= len(course_holes) / 2:
            OSM_NAME = max(named)[1]
            print(f"course name from the map: {OSM_NAME}")

    def inside(geom):
        out = np.zeros(len(P), bool)
        if geom.is_empty:
            return out
        b = geom.bounds
        idx = np.where((P[:, 0] >= b[0]) & (P[:, 0] <= b[2]) & (P[:, 1] >= b[1]) & (P[:, 1] <= b[3]))[0]
        shapely.prepare(geom)
        out[idx] = shapely.contains_xy(geom, P[idx, 0], P[idx, 1])
        return out
    osm_owned = ~inside(keep_old)
    CLS[osm_owned & np.isin(CLS, (1, 2, 3, 4, 5, 7, 8, 9))] = 0
    for layer, cls in (("beach", 11), ("rough", 1), ("fairway", 2), ("fringe", 8), ("green", 3), ("tee", 4),
                       ("bunker", 5), ("water", 13), ("path", 7), ("building", 14)):
        geom = OSM_L["green"].buffer(1.6) if layer == "fringe" else OSM_L[layer]
        m = osm_owned & inside(geom)
        if layer == "rough":
            m &= CLS != 6   # GKD penalty areas stay
        CLS[m] = cls
    OSM_L["keep_old"] = keep_old

# real ground: rock on steep natural slopes, a shoreline strip, and the sea
if DEM:
    hh = H
    dzdx = ((hh[:-1, 1:] - hh[:-1, :-1]) + (hh[1:, 1:] - hh[1:, :-1])) / (2 * CELL)
    dzdz = ((hh[1:, :-1] - hh[:-1, :-1]) + (hh[1:, 1:] - hh[:-1, 1:])) / (2 * CELL)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdz)))
    cell_min = np.minimum.reduce([hh[:-1, :-1], hh[1:, :-1], hh[:-1, 1:], hh[1:, 1:]])
    cell_max = np.maximum.reduce([hh[:-1, :-1], hh[1:, :-1], hh[:-1, 1:], hh[1:, 1:]])
    per_tri = lambda a: np.repeat(a.ravel(), 2)
    natural = np.isin(CLS, (0, 1, 9))
    CLS[natural & (per_tri(slope) > 38)] = 10
    if SEA is not None:
        CLS[np.isin(CLS, (0, 1, 9, 10)) & (per_tri(cell_min) < SEA + 2.2) & (per_tri(cell_max) > SEA - 1)] = 11
        CLS[per_tri(cell_max) < SEA - 1] = 12
    print(f"rock {np.mean(CLS == 10) * 100:.1f}%, shore {np.mean(CLS == 11) * 100:.1f}%, sea {np.mean(CLS == 12) * 100:.1f}% of triangles")

# ---------------------------------------------------------------- what to draw
# The grid is a rectangle around the course, and one that winds through houses takes in whole neighbourhoods.
# Only ground within MARGIN of the golf is drawn (shots, the course file's tees, pins, hazards and bounds, and
# the mapped fairways, greens, tees and bunkers), with the trees and buildings on it. Small pockets enclosed
# by the course stay, so there are no pits between holes. A course by the sea keeps it all, sea and all.
MARGIN, POCKET_M2 = 100.0, 40000.0
KEEP = None
if SEA is None:
    from scipy import ndimage as _ndi
    CX, CZ = np.meshgrid(x0 + (np.arange(nx - 1) + 0.5) * CELL, z0 + (np.arange(nz - 1) + 0.5) * CELL)
    centres = np.stack([CX.ravel(), CZ.ravel()], 1)
    golf_pts = np.vstack([S[:, :2]] + ([track_xz] if len(track_xz) else []))
    cell_keep = cKDTree(golf_pts).query(centres, distance_upper_bound=MARGIN)[0] < MARGIN
    if OSM_L is not None:
        golf = shapely.union_all([OSM_L[k] for k in ("fairway", "green", "tee", "bunker")])
        if not golf.is_empty:
            cell_keep |= shapely.contains_xy(golf.buffer(MARGIN, quad_segs=4), centres[:, 0], centres[:, 1])
    cell_keep = cell_keep.reshape(nz - 1, nx - 1)
    gaps, n_gaps = _ndi.label(~cell_keep)
    if n_gaps:
        size = np.bincount(gaps.ravel())
        edge = np.unique(np.concatenate([gaps[0], gaps[-1], gaps[:, 0], gaps[:, -1]]))
        small = size * CELL * CELL < POCKET_M2
        small[0] = False; small[edge] = False
        cell_keep |= small[gaps]
    if not cell_keep.all():
        KEEP = np.repeat(cell_keep.ravel(), 2)          # per triangle, two to a cell, as CLS
        print(f"drawing {cell_keep.mean() * 100:.0f}% of the ground: within {MARGIN:.0f} m of the golf")

# ---------------------------------------------------------------- trees
tree_r = None
if TREES:
    # real trees found in the lidar point cloud: position, height, crown radius and shape
    tj = json.load(open(TREES))
    TX = np.array(tj["x"]) - OX; TZ = -(np.array(tj["z"]) - OZ)
    inb = (TX > x0 + 1) & (TX < x1 - 1) & (TZ > z0 + 1) & (TZ < z1 - 1)
    fx_, fz_ = (TX - x0) / CELL, (TZ - z0) / CELL
    ci = ((np.clip(fz_.astype(int), 0, nz - 2) * (nx - 1) + np.clip(fx_.astype(int), 0, nx - 2)) * 2 + ((fx_ % 1) + (fz_ % 1) > 1)).astype(int)
    ok = inb & ~np.isin(CLS[np.clip(ci, 0, len(CLS) - 1)], (3, 4, 12, 13, 14))     # not on a green, tee, the sea, water or a roof
    if KEEP is not None:
        ok &= KEEP[np.clip(ci, 0, len(CLS) - 1)]
    cand = np.stack([TX[ok], TZ[ok]], 1)
    th = np.array(tj["h"])[ok]; tree_r = np.array(tj["r"])[ok]
    kind = (1 - np.array(tj["kind"])[ok]).astype(np.uint8)                      # viewer: 0 cone (columnar), 1 crown (spreading)
    ty = tri_sample(H, cand[:, 0], cand[:, 1])
    print(f"trees: {len(cand)} from lidar ({int((~ok & inb).sum())} skipped on greens, tees, water or roofs)")
    tree_arr = np.stack([np.round((cand[:, 0] - x0) * 10), np.round((cand[:, 1] - z0) * 10), np.round(ty * 10), np.round(th * 10)], 1).astype(np.int16)
if not TREES:
    area = (x1 - x0) * (z1 - z0)
    ncand = int(area / 140)
    cand = np.stack([rng.uniform(x0 + 15, x1 - 15, ncand), rng.uniform(z0 + 15, z1 - 15, ncand)], 1)
    cc = CLS[near_tree.query(cand)[1]]
    if KEEP is not None:
        cc = np.where(KEEP[near_tree.query(cand)[1]], cc, -1)    # no trees off the drawn ground
    woods = vnoise(18)
    wi = np.clip(((cand[:, 0] - x0) / CELL).astype(int), 0, nx - 1); wj = np.clip(((cand[:, 1] - z0) / CELL).astype(int), 0, nz - 1)
    dens = woods[wj, wi]
    dtrack = cKDTree(track_xz).query(cand)[0]
    ok = (((cc == 0) & (dens > -0.3)) | ((cc == 9) & (dens > 0.9)) | ((cc == 6) & (dens > 0.2))) & (dtrack > 16)
    cand = cand[ok]
    th = rng.uniform(8, 18, len(cand)); kind = (rng.random(len(cand)) < 0.6).astype(np.uint8)
    ty = tri_sample(H, cand[:, 0], cand[:, 1])
    print("trees:", len(cand))
    tree_arr = np.stack([np.round((cand[:, 0] - x0) * 10), np.round((cand[:, 1] - z0) * 10), np.round(ty * 10), np.round(th * 10)], 1).astype(np.int16)

# ---------------------------------------------------------------- buildings
def tag_height(t):
    """A building's height from its map tags, or a typical one- to two-storey height."""
    try:
        return float(re.match(r"[\d.]+", str(t["height"])).group())
    except (KeyError, AttributeError, ValueError):
        pass
    try:
        return float(re.match(r"[\d.]+", str(t["building:levels"])).group()) * 3 + 1
    except (KeyError, AttributeError, ValueError):
        return 3.0 if t.get("building") in ("shed", "garage", "garages", "hut", "roof", "carport") else 4.5


buildings, measured = [], 0
if OSM_L is not None:
    roofs = json.load(open(BUILDINGS)) if BUILDINGS and os.path.exists(BUILDINGS) else {}
    for t, g in F:
        if "building" not in t or t["building"] == "no" or g.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        g = shapely.transform(g, to_view)
        for part in (g.geoms if g.geom_type == "MultiPolygon" else [g]):
            b = part.bounds
            if part.area < 12 or b[0] < x0 + 2 or b[1] < z0 + 2 or b[2] > x1 - 2 or b[3] > z1 - 2:
                continue
            ring = np.array(part.simplify(0.4).exterior.coords)[:-1]
            if len(ring) < 3 or (KEEP is not None and not KEEP[near_tree.query(ring)[1]].all()):   # wholly on drawn ground
                continue
            ground = tri_sample(H, ring[:, 0], ring[:, 1])
            roof = roofs.get(t["@id"])
            measured += bool(roof)
            h = np.clip(roof["h"] if roof else tag_height(t), 2.5, 120.0)
            buildings.append({"p": [round(float(v), 1) for v in ring.ravel()], "b": round(float(ground.min()) - 0.5, 1),
                              "t": round(float(np.median(ground) + h), 1)})
    print(f"buildings: {len(buildings)} on the course's ground, {measured} with measured heights")

# ---------------------------------------------------------------- tee-shot images
images = {}
try:
    from PIL import Image
    for ch in course_holes:
        f = os.path.join(IMGDIR, "TeeShot", f"TeeShot_Hole{ch['n']}.jpg")
        if os.path.exists(f):
            im = Image.open(f).convert("RGB")
            w, h = im.size
            im = im.crop((0, int(h * 0.05), w, h)).resize((1120, int(1120 * (h * 0.95) / w)), Image.LANCZOS)
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=70, optimize=True, progressive=True)
            images[str(ch["n"])] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
except ImportError:
    pass
print("images:", len(images), sum(len(v) for v in images.values()) // 1024, "KB")


# ---------------------------------------------------------------- surface texture (OSM courses)
PALETTE = {0: "#8E9F63", 1: "#6E9E48", 2: "#8BC756", 3: "#A6DB63", 4: "#93CC5A", 5: "#E9DAA8", 6: "#7C7146", 7: "#CFC8B8",
           8: "#98D15E", 9: "#5B8A3E", 10: "#9A8670", 11: "#D9C99A", 12: "#3F7F86", 13: "#4A9CC4", 14: "#B9A48E"}


def surface_texture():
    """OSM shapes drawn crisply at about 1 m a pixel over the ground's own classes (rock, shore, penalty areas)."""
    import shapely
    from PIL import Image, ImageDraw
    rgb = lambda h: tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    res = max(1.0, max(x1 - x0, z1 - z0) / 4000.0)   # metres per pixel; stays within 4096 px for phones
    ss = 2                                             # drawn at twice that, then averaged down for clean edges
    Wp, Hp = int(math.ceil((x1 - x0) / res)), int(math.ceil((z1 - z0) / res))
    # base layer: each pixel takes its triangle's class; course surfaces OSM now owns show as plain ground
    X = x0 + (np.arange(Wp) + 0.5) * res; Z = z0 + (np.arange(Hp) + 0.5) * res
    fx = np.clip((X - x0) / CELL, 0, nx - 1.0001); fz = np.clip((Z - z0) / CELL, 0, nz - 1.0001)
    i = fx.astype(int)[None, :]; j = fz.astype(int)[:, None]
    up = (fx - np.floor(fx))[None, :] + (fz - np.floor(fz))[:, None] > 1
    tri = (j * (nx - 1) + i) * 2 + up
    base = CLS[tri]
    base = np.where(osm_owned[tri] & np.isin(base, (1, 2, 3, 4, 5, 7, 8, 13, 14)), 0, base)
    lut = np.array([rgb(PALETTE.get(k, PALETTE[0])) for k in range(16)], dtype=np.uint8)
    img = Image.fromarray(lut[base]).resize((Wp * ss, Hp * ss), Image.NEAREST)
    A = np.asarray(img).copy()
    W2, H2, k = Wp * ss, Hp * ss, ss / res

    def mask(geom):
        """Even-odd fill of every ring, so holes come out right."""
        m = np.zeros((H2, W2), bool)
        if geom.is_empty:
            return m
        for part in getattr(geom, "geoms", [geom]):
            if part.geom_type != "Polygon":
                continue
            for ring in [part.exterior, *part.interiors]:
                c = np.asarray(ring.coords)
                px, py = (c[:, 0] - x0) * k, (c[:, 1] - z0) * k
                bx0, by0 = max(int(px.min()) - 1, 0), max(int(py.min()) - 1, 0)
                bx1, by1 = min(int(px.max()) + 2, W2), min(int(py.max()) + 2, H2)
                if bx1 <= bx0 or by1 <= by0:
                    continue
                im = Image.new("1", (bx1 - bx0, by1 - by0), 0)
                ImageDraw.Draw(im).polygon(list(zip(px - bx0, py - by0)), fill=1)
                m[by0:by1, bx0:bx1] ^= np.asarray(im, dtype=bool)
        return m

    def fill(geom, color, shade=None):
        geom = geom.difference(OSM_L["keep_old"]) if not OSM_L["keep_old"].is_empty else geom
        m = mask(geom)
        if shade is None:
            A[m] = rgb(color)
        else:
            A[m] = np.clip(np.array(rgb(color))[None, :] * shade[m][:, None], 0, 255).astype(np.uint8)
    L = OSM_L
    fill(L["beach"], PALETTE[11])
    fill(L["rough"], PALETTE[1])
    # fairways mown in stripes along each fairway's long axis
    for fw in L["fairways"]:
        mrr = np.asarray(fw.minimum_rotated_rectangle.exterior.coords)
        e = [mrr[1] - mrr[0], mrr[2] - mrr[1]]
        along = max(e, key=lambda v: np.hypot(*v)); along = along / (np.hypot(*along) + 1e-9)
        b = fw.bounds
        bx0, by0 = max(int((b[0] - x0) * k) - 1, 0), max(int((b[1] - z0) * k) - 1, 0)
        bx1, by1 = min(int((b[2] - x0) * k) + 2, W2), min(int((b[3] - z0) * k) + 2, H2)
        if bx1 <= bx0 or by1 <= by0:
            continue
        sub = mask(fw)[by0:by1, bx0:bx1]
        gy, gx = np.mgrid[by0:by1, bx0:bx1]
        Xs, Zs = (gx + 0.5) / k + x0, (gy + 0.5) / k + z0
        band = (np.floor((-Xs * along[1] + Zs * along[0]) / 7.5) % 2).astype(bool)
        col = np.where(band[..., None], np.array(rgb(PALETTE[2])) * 1.05, np.array(rgb(PALETTE[2])) * 0.94)
        A[by0:by1, bx0:bx1][sub] = np.clip(col[sub], 0, 255).astype(np.uint8)
    fill(L["path"], PALETTE[7])
    fill(L["green"].buffer(1.6), PALETTE[8])
    fill(L["green"], PALETTE[3])
    fill(L["tee"], PALETTE[4])
    fill(L["water"].buffer(0.8), "#3A7FA6"); fill(L["water"], PALETTE[13])
    fill(L["bunker"].buffer(0.6), "#C7B686"); fill(L["bunker"], PALETTE[5])
    fill(L["building"].buffer(0.5), "#8E7A66"); fill(L["building"], PALETTE[14])
    out = Image.fromarray(A).resize((Wp, Hp), Image.BOX)
    buf = io.BytesIO(); out.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    print(f"surface texture {Wp}x{Hp} at {res:.2f} m/px, {len(data) // 1024} KB")
    return {"src": "data:image/png;base64," + base64.b64encode(data).decode(), "w": round(Wp * res, 3), "h": round(Hp * res, 3)}


TEX = surface_texture() if OSM_L is not None else None


# ---------------------------------------------------------------- pack
def played():
    """(epoch ms, local calendar day) for when the round was played. The day is taken in this
    machine's time zone, which on the GSPro PC is the player's, so every viewer sees the same date."""
    import datetime as dt
    if PLAYED and not PLAYED.isdigit():   # an explicit calendar date
        d = dt.datetime.strptime(PLAYED, "%Y-%m-%d")
        return int(d.replace(hour=12, tzinfo=dt.timezone.utc).timestamp() * 1000), d.strftime("%Y-%m-%d")
    ms = int(PLAYED) if PLAYED else int(os.path.getmtime(ROUND) * 1000)
    return ms, dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")



def pack_item(it):
    if it["kind"] in ("shot", "mulligan"):
        return {
            "kind": it["kind"], "stroke": it["stroke"], "gsn": it["gsn"], "club": it["club"], "putt": it["putt"],
            "flight": [r1(q) for q in it["flight"]], "flightT": round(it["flightT"], 3),
            "roll": [r1(q) + [round(t, 3)] for q, t in it["roll"]],
            "start": r1(it["start"]), "end": r1(it["end"]), "td": r1(it["td"]),
            "fromLie": it["fromLie"], "toLie": it["toLie"], "tdLie": it["tdLie"],
            "toPinM": round(it["toPin"], 2), "mph": round(it["mph"], 1), "vla": round(it["vla"], 1),
            "hla": round(it["hla"], 1), "spin": round(it["spin"]), "axis": round(it["axis"], 1),
            "apexFt": round(it["apexFt"]), "pen": it["pen"], "ob": it.get("ob", False), "holed": it["holed"],
        }
    if it["kind"] == "penalty":
        return {"kind": "penalty", "stroke": it["stroke"], "gsn": it["gsn"], "drop": r1(it["drop"]), "dropLie": it["dropLie"], "toPinM": round(it["toPin"], 2), "ob": it.get("ob", False)}
    return {"kind": "putt", "stroke": it["stroke"], "gsn": it["gsn"], "holed": it["holed"],
            **({"fromM": round(it["fromM"], 2)} if "fromM" in it else {})}


white_sr = (gkd or {}).get(f"{(tee_types[0] or 'White')}SR") if gkd else None
data = {
    "course": {
        "key": rows[0]["CourseKey"],
        # no course file: tidy the folder key, e.g. HobbleCreek_GSP -> Hobble Creek
        "name": gkd.get("CourseName") if gkd and gkd.get("CourseName") else OSM_NAME or re.sub(r"(?<=[a-z])(?=[A-Z])", " ", re.sub(r"_GSP$", "", rows[0]["CourseKey"])).replace("_", " ").strip(),
        "gkd": bool(gkd),
        "location": (gkd or {}).get("Location") or "", "designer": (gkd or {}).get("Designer") or "",
        "par": sum(ch["par"] for ch in course_holes),
        "teeType": max(set(t for t in tee_types if t), key=tee_types.count) if any(tee_types) else None,
        "pinDay": max(set(p for p in pin_days if p), key=pin_days.count) if any(pin_days) else None,
        "rating": white_sr,
        "holes": [{"n": ch["n"], "par": ch["par"], "index": ch["index"], "yards": round(ch["yards"]),
                   "tee": r1(ch["tee"]), "pin": r1(ch["pin"]), "aims": [r1(a) for a in ch["aims"]],
                   "pinFromCourse": ch.get("pinDay") is not None} for ch in course_holes],
    },
    "roundId": rows[0]["RoundID"],
    "played": played()[0], "playedDay": played()[1],
    "players": [{"name": p["name"], "holes": [None if h is None else {"strokes": h["strokes"], "complete": h["complete"], "items": [pack_item(i) for i in h["items"]]} for h in p["holes"]]} for p in players],
    "terrain": {"x0": round(x0, 2), "z0": round(z0, 2), "cell": CELL, "nx": nx, "nz": nz,
                "source": "lidar" if DEM else "fitted", "credit": _opts.get("terrain-credit"), "sea": None if SEA is None else round(float(SEA), 2), "tex": TEX,
                "h": base64.b64encode(np.round(H * 10).astype(np.int16).tobytes()).decode(),
                # cells to draw, one bit each, row by row; absent means all of them
                "keep": None if KEEP is None else base64.b64encode(np.packbits(KEEP[::2])).decode(),
                "cls": None if TEX else base64.b64encode(CLS.tobytes()).decode()},
    "trees": {"n": len(cand), "xyzh": base64.b64encode(tree_arr.tobytes()).decode(), "kind": base64.b64encode(kind.tobytes()).decode(),
              "r": None if tree_r is None else base64.b64encode(np.clip(np.round(tree_r * 10), 1, 255).astype(np.uint8).tobytes()).decode(),
              "source": "lidar" if TREES else "placeholder", "credit": _opts.get("trees-credit")},
    "buildings": {"items": buildings, "measured": measured},
    "stakes": {"red": [r1(s) for s in stakes_red], "white": [r1(s) for s in stakes_white]},
    "hazards": [[r1(xz(q)) for q in poly] for poly in hazards],
    "images": images,
}
json.dump(data, open(OUT, "w", encoding="utf-8"), separators=(",", ":"))
print("wrote", OUT, os.path.getsize(OUT) // 1024, "KB")
for p in data["players"]:
    tot = sum(h["strokes"] for h in p["holes"] if h and h["complete"])
    par = sum(ch["par"] for ch, h in zip(data["course"]["holes"], p["holes"]) if h and h["complete"])
    print(f"  {p['name']}: {tot} on par {par} ({tot - par:+d}); mulligans {sum(1 for h in p['holes'] if h for i in h['items'] if i['kind'] == 'mulligan')}; real putts {sum(1 for h in p['holes'] if h for i in h['items'] if i['kind'] == 'shot' and i['putt'])}")
