"""Line up public lidar elevation with a GSPro course's own coordinates.

    python align_lidar.py ROUND.dat COURSE.GKD OUT.json DEM.tif [DEM2.tif ...]

GSPro courses live in Unity world coordinates (metres, Y up) with no link to the real world.
Every ball rest, roll sample, tee, pin and hazard outline in the round and course files is a
point where we know GSPro's ground height. This finds the placement of those points on the lidar
elevation model that makes the heights agree: first a brute-force search over every position and
the eight axis orientations (done with FFT correlation), then a least-squares fit of position,
small rotation, scale and a height offset. OUT.json holds the transform and how well it fits.
"""
import json, sys
import numpy as np
import rasterio
from rasterio.merge import merge
from scipy.ndimage import gaussian_filter, map_coordinates
from scipy.optimize import least_squares
from scipy.signal import fftconvolve

COARSE = 2.0   # metres per cell for the brute-force search


def ground_points(rows, gkd):
    """(x, y, z, weight, kind) for every place GSPro's ground height is known."""
    pts = []
    add = lambda p, w, k: pts.append((p["x"], p["y"], p["z"], w, k))
    for r in rows:
        add(r["StartingPOS"], 1.0, "ball")
        if r.get("EndingPOS"):
            add(r["EndingPOS"], 1.0, "ball")
        path = (r.get("activeShot") or {}).get("sd", {}).get("path") or []
        for q in path[int(len(path) * 0.3):]:   # skip the early bounces
            add(q["pos"], 0.5, "roll")
    if gkd:
        for h in gkd["Holes"]:
            for p in h.get("Pins") or []:
                if p.get("Position"):
                    add(p["Position"], 1.0, "pin")
            for t in h["Tees"]:
                if t.get("Enabled") and t.get("Position"):
                    add(t["Position"], 0.5, "tee" if "Aim" not in t["TeeType"] and "Center" not in t["TeeType"] else "aim")
        for hz in gkd.get("Hazards") or []:
            for p in hz["coords"]:
                add(p, 0.3, "hazard")
        for p in (gkd.get("pOOB") or {}).get("coords") or []:
            add(p, 0.3, "oob")
    a = np.array([p[:4] for p in pts], dtype=float)
    kinds = np.array([p[4] for p in pts])
    # one point per 1 m cell and kind, so dense roll paths don't swamp everything else
    key = np.c_[np.round(a[:, 0]), np.round(a[:, 2]), np.unique(kinds, return_inverse=True)[1]]
    _, first = np.unique(key, axis=0, return_index=True)
    return a[first], kinds[first]


def load_dem(paths):
    """Merged DEM as a north-up grid G[row_north, col_east] with cell centres at E0 + i*res, N0 + j*res."""
    srcs = [rasterio.open(p) for p in paths]
    arr, tr = merge(srcs, nodata=np.nan) if len(srcs) > 1 else (srcs[0].read(1, masked=True).filled(np.nan)[None].astype(np.float32), srcs[0].transform)
    crs = srcs[0].crs.to_string() if srcs[0].crs else None
    Z = arr[0].astype(np.float32)
    nod = srcs[0].nodata
    if nod is not None:
        Z[Z == nod] = np.nan
    Z[Z < -1000] = np.nan
    res_e, res_n = tr.a, -tr.e
    assert abs(tr.b) < 1e-9 and abs(tr.d) < 1e-9, "rotated rasters not supported"
    E0 = tr.c + res_e / 2
    N_top = tr.f - res_n / 2
    G = Z[::-1]                                  # row 0 = southernmost
    N0 = N_top - (Z.shape[0] - 1) * res_n
    for s in srcs:
        s.close()
    return G, E0, N0, res_e, res_n, crs


def block_mean(G, k):
    if k <= 1:
        return G
    h, w = (G.shape[0] // k) * k, (G.shape[1] // k) * k
    B = G[:h, :w].reshape(h // k, k, w // k, k)
    return np.nanmean(B, axis=(1, 3))


VARIANTS = [(swap, sx, sz) for swap in (0, 1) for sx in (1, -1) for sz in (1, -1)]


def orient(x, z, v):
    swap, sx, sz = v
    a, b = sx * x, sz * z
    return (b, a) if swap else (a, b)   # (east-ish, north-ish)


def coarse_search(P, G, res):
    """Best integer shift for each axis orientation: minimise the weighted variance of (y - ground)."""
    W, Y = P[:, 3], P[:, 1]
    Gf = np.where(np.isnan(G), np.nanmin(G) - 60.0, G).astype(np.float64)   # no-data (sea) reads as deep water
    G2 = Gf * Gf
    sw, swy, swy2 = W.sum(), (W * Y).sum(), (W * Y * Y).sum()
    out = []
    for v in VARIANTS:
        u, w_ = orient(P[:, 0], P[:, 2], v)
        ku = np.round((u - u.min()) / res).astype(int); kv = np.round((w_ - w_.min()) / res).astype(int)
        shape = (kv.max() + 1, ku.max() + 1)
        if shape[0] >= G.shape[0] or shape[1] >= G.shape[1]:
            continue
        Kw = np.zeros(shape); Kwy = np.zeros(shape)
        np.add.at(Kw, (kv, ku), W); np.add.at(Kwy, (kv, ku), W * Y)
        corr = lambda A, K: fftconvolve(A, K[::-1, ::-1], mode="valid")
        sG, sG2, syG = corr(Gf, Kw), corr(G2, Kw), corr(Gf, Kwy)
        mean_d = (swy - sG) / sw
        var = (swy2 - 2 * syG + sG2) / sw - mean_d ** 2
        j, i = np.unravel_index(np.argmin(var), var.shape)
        # how distinct is the winner? best variance more than 30 m away from it
        far = var.copy(); r = int(30 / res); far[max(0, j - r):j + r + 1, max(0, i - r):i + r + 1] = np.inf
        out.append(dict(v=v, std=float(np.sqrt(max(var[j, i], 0))), runner=float(np.sqrt(max(far.min(), 0))),
                        dy=float(mean_d[j, i]), i=int(i), j=int(j), umin=float(u.min()), vmin=float(w_.min())))
    return sorted(out, key=lambda o: o["std"])


def main():
    ROUND, GKD, OUT, *DEMS = sys.argv[1:]
    rows = json.load(open(ROUND, encoding="utf-8-sig"))
    gkd = json.load(open(GKD, encoding="utf-8-sig")) if GKD != "-" else None
    P, kinds = ground_points(rows, gkd)
    print(f"{len(P)} ground points: " + ", ".join(f"{k} {int((kinds == k).sum())}" for k in np.unique(kinds)))
    G, E0, N0, re, rn, crs = load_dem(DEMS)
    print(f"DEM {G.shape[1]} x {G.shape[0]} cells at {re:g} m ({crs}); {np.isnan(G).mean() * 100:.0f}% no data")

    k = max(1, int(round(COARSE / re)))
    Gc = block_mean(G, k); rc = re * k
    ranked = coarse_search(P, Gc, rc)
    for o in ranked[:3]:
        print(f"  orientation {o['v']}: spread {o['std']:.2f} m (next-best placement {o['runner']:.2f} m)")
    best = ranked[0]
    u, v = orient(P[:, 0], P[:, 2], best["v"])
    tE = E0 + (k - 1) * re / 2 + best["i"] * rc - best["umin"]
    tN = N0 + (k - 1) * rn / 2 + best["j"] * rc - best["vmin"]

    # fine fit: shift, small rotation, scale, height scale and offset. The raw 0.5 m surface is full of
    # small bumps that trap a local optimiser, so: a fine shift search on smoothed ground, a fit on the
    # smoothed ground, then a final fit on the raw lidar.
    cu, cv = np.average(u, weights=P[:, 3]), np.average(v, weights=P[:, 3])
    Gn = np.where(np.isnan(G), np.nanmin(G) - 60.0, G)
    Gs = gaussian_filter(Gn, 1.5 / re)

    def ground(p, uu, vv, grid):
        dE, dN, th, s = p[:4]
        c, sn = np.cos(th), np.sin(th)
        E = tE + cu + dE + s * (c * (uu - cu) - sn * (vv - cv))
        N = tN + cv + dN + s * (sn * (uu - cu) + c * (vv - cv))
        return map_coordinates(grid, [(N - N0) / rn, (E - E0) / re], order=1, mode="nearest")

    W = P[:, 3]
    def spread(dE, dN):
        r = P[:, 1] - ground([dE, dN, 0, 1], u, v, Gs)
        m = np.average(r, weights=W)
        return np.average((r - m) ** 2, weights=W), m
    steps = np.arange(-3, 3.001, 0.25)
    cands = [(spread(a, b_)[0], a, b_) for a in steps for b_ in steps]
    _, dE0, dN0 = min(cands)
    p = np.array([dE0, dN0, 0, 1, 1, spread(dE0, dN0)[1]], dtype=float)
    for grid, fs in ((Gs, 0.5), (Gn, 0.3)):
        fit = least_squares(lambda q: np.sqrt(W) * (P[:, 1] - (q[4] * ground(q, u, v, grid) + q[5])), p,
                            loss="soft_l1", f_scale=fs, x_scale=[1, 1, 0.01, 0.01, 0.01, 1])
        p = fit.x
    ground_raw = lambda p, uu, vv: ground(p, uu, vv, Gn)
    r = P[:, 1] - (p[4] * ground_raw(p, u, v) + p[5])
    stats = {kd: {"n": int((kinds == kd).sum()), "median_abs_m": round(float(np.median(np.abs(r[kinds == kd]))), 3),
                  "p90_abs_m": round(float(np.percentile(np.abs(r[kinds == kd]), 90)), 3)} for kd in np.unique(kinds)}
    print(f"fine fit: shift {p[0]:+.2f} / {p[1]:+.2f} m, rotation {np.degrees(p[2]):+.3f} deg, scale {p[3]:.5f}, "
          f"height = {p[4]:.4f} x lidar {p[5]:+.2f} m")
    for kd, s in stats.items():
        print(f"  {kd:>6}: {s['n']:>4} points, median |error| {s['median_abs_m']:.2f} m, 90% within {s['p90_abs_m']:.2f} m")
    out = {"crs": crs, "orientation": {"swap": best["v"][0], "sx": best["v"][1], "sz": best["v"][2]},
           "tE": tE + cu + p[0], "tN": tN + cv + p[1], "cu": cu, "cv": cv, "theta": p[2], "scale": p[3],
           "hScale": p[4], "hOffset": p[5], "coarseSpread": best["std"], "coarseRunnerUp": best["runner"], "fit": stats,
           "note": "E,N = (tE,tN) + scale*R(theta)*((u,v)-(cu,cv)), with (u,v) from GSPro (x,z) by orientation; GSPro y = hScale*lidar + hOffset"}
    json.dump(out, open(OUT, "w"), indent=1)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
