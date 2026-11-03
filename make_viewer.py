"""Build a standalone 3D round-replay HTML page from a GSPro currentRound.dat.

    python make_viewer.py --round "C:/Users/<you>/AppData/LocalLow/GSPro/GSPro/currentRound.dat" \
                          --course "C:/GSProV1/Core/GSP/Courses/Manele_Golf_Course_GSP" \
                          --out manele-round-121.html [--date 2026-10-26] \
                          [--lidar lidar/manele_lidar.tif --align lidar/manele_align.json] \
                          [--osm osm/manele_golf.json --osm osm/manele_cover.json] \
                          [--trees lidar/manele_trees.json]

--course is optional. With it, tees, pins, aim lines, penalty areas, the boundary and
tee-shot images come from the course's .GKD file and CourseImages folder; without it the
page still works from the shot data alone. With --lidar (a lidar elevation GeoTIFF covering the
course), the ground is the real terrain, lined up to GSPro's coordinates automatically; the
alignment is saved to --align (default: <lidar>.align.json) and reused next time. Lidar needs
rasterio (pip install rasterio). With --osm (OpenStreetMap exports from fetch_osm.py) and --lidar,
fairways, greens, tees, bunkers, water, paths and buildings are drawn from OpenStreetMap; the small
shift that lines the map up with the course is saved to --osm-shift (default: next to the first
--osm file) and reused. OSM needs shapely (pip install shapely). With --trees (from lidar_trees.py),
the trees are the real ones found in the lidar point cloud instead of placeholders. The round date shown on the page is the time GSPro
last wrote currentRound.dat (the final shot), unless --date gives one. Needs Python 3.9+ with
numpy, scipy and Pillow.
"""
import argparse, json, os, subprocess, sys, tempfile

here = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--round", required=True, help="path to currentRound.dat")
ap.add_argument("--course", help="GSPro course folder (the one holding the .GKD)")
ap.add_argument("--out", help="output .html (default: <course>-round-<id>.html next to this script)")
ap.add_argument("--date", help="date the round was played, YYYY-MM-DD (default: when currentRound.dat was last written)")
ap.add_argument("--lidar", action="append", help="lidar elevation GeoTIFF for real ground (repeat for several tiles)")
ap.add_argument("--align", help="alignment file for --lidar (created if missing)")
ap.add_argument("--osm", action="append", help="OpenStreetMap export from fetch_osm.py (repeat for each file)")
ap.add_argument("--osm-shift", help="shift file for --osm (created if missing)")
ap.add_argument("--trees", help="real trees from lidar_trees.py")
a = ap.parse_args()

gkd, imgs = "-", "-"
if a.course:
    found = [f for f in os.listdir(a.course) if f.upper().endswith(".GKD")]
    if found:
        gkd = os.path.join(a.course, found[0])
    imgs = os.path.join(a.course, "CourseImages")
tmp = os.path.join(tempfile.gettempdir(), "retrace_round.json")
extra = [a.date] if a.date else []
if a.lidar:
    extra += ["--dem=" + ",".join(a.lidar), "--align=" + (a.align or os.path.splitext(a.lidar[0])[0] + ".align.json")]
if a.trees:
    extra += ["--trees=" + a.trees]
if a.osm:
    extra += ["--osm=" + ",".join(a.osm), "--osm-shift=" + (a.osm_shift or os.path.splitext(a.osm[0])[0].replace("_golf", "") + "_shift.json")]
subprocess.run([sys.executable, os.path.join(here, "build_round.py"), a.round, gkd, imgs, tmp] + extra, check=True)
data = open(tmp, encoding="utf-8").read()
meta = json.loads(data)
out = a.out or os.path.join(here, f"{meta['course']['key'].replace('_GSP', '').lower()}-round-{meta['roundId']}.html")
tpl = open(os.path.join(here, "viewer.template.html"), encoding="utf-8").read()
# a complete page (doctype, UTF-8, viewport) so it opens straight from disk or from a Cloud Storage bucket
head = ('<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
page = head + tpl.replace("__ROUND_DATA__", data.replace("</", "<\\/")) + "\n</html>\n"
open(out, "w", encoding="utf-8").write(page)
print("wrote", out)
