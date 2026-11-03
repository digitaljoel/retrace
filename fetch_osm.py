"""Download a course's OpenStreetMap features for make_viewer.py --osm.

    python fetch_osm.py --course "C:/GSProV1/Core/GSP/Courses/Manele_Golf_Course_GSP" --out osm/manele
    python fetch_osm.py --lat 40.16083 --lon -111.51211 --out osm/hobblecreek      (no course folder)

Reads the course's latitude/longitude from its .GKD and asks the Overpass API for everything golf
(fairways, greens, tees, bunkers, rough, paths) plus land cover, water, roads and buildings in a
5.2 x 4 km box around it. Writes <out>_golf.json and <out>_cover.json. Only real-world courses
that someone has mapped in OpenStreetMap will have golf features. Map data (c) OpenStreetMap
contributors, ODbL.
"""
import argparse, json, math, os, time, urllib.error, urllib.parse, urllib.request

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--course", help="GSPro course folder (the one holding the .GKD); or give --lat and --lon")
ap.add_argument("--lat", type=float, help="course latitude, when there's no course folder")
ap.add_argument("--lon", type=float, help="course longitude, when there's no course folder")
ap.add_argument("--out", required=True, help="output prefix, e.g. osm/manele")
ap.add_argument("--half-width", type=float, default=2600, help="metres east and west of the course point")
ap.add_argument("--half-height", type=float, default=2000, help="metres north and south of the course point")
a = ap.parse_args()

if a.course:
    gkd_file = next(f for f in os.listdir(a.course) if f.upper().endswith(".GKD"))
    g = json.load(open(os.path.join(a.course, gkd_file), encoding="utf-8-sig"))
    lat, lon = g["CourseLat"], g["CourseLong"]
elif a.lat is not None and a.lon is not None:
    gkd_file, g, lat, lon = "course", {}, a.lat, a.lon
else:
    ap.error("give --course, or --lat and --lon")
dlat = a.half_height / 111320.0
dlon = a.half_width / (111320.0 * math.cos(math.radians(lat)))
bb = f"{lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f}"
print(f"{g.get('CourseName', gkd_file)} at {lat:.5f}, {lon:.5f}; box {bb}")

QUERIES = {
    "golf": f'nwr["golf"]({bb});nwr["leisure"="golf_course"]({bb});',
    "cover": f'nwr["natural"]({bb});nwr["landuse"]({bb});nwr["water"]({bb});way["highway"]({bb});nwr["building"]({bb});',
}
os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
def overpass(q):
    url = "https://overpass-api.de/api/interpreter?" + urllib.parse.urlencode({"data": f"[out:json][timeout:120];({q});out geom;"})
    # Overpass turns away requests that don't say who they are, and is often busy: back off and retry
    req = urllib.request.Request(url, headers={"User-Agent": "gspro-round-viewer/0.1 (personal golf replay viewer)", "Accept": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=200) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 502, 503, 504) or attempt == 4:
                raise
            wait = 15 * (attempt + 1)
            print(f"  Overpass busy (HTTP {e.code}); retrying in {wait} s")
            time.sleep(wait)


for name, q in QUERIES.items():
    data = overpass(q)
    path = f"{a.out}_{name}.json"
    open(path, "wb").write(data)
    els = json.loads(data)["elements"]
    golf = sum(1 for e in els if "golf" in e.get("tags", {}))
    print(f"wrote {path}: {len(els)} features" + (f", {golf} golf" if name == "golf" else ""))
