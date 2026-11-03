# Retrace (prototype)

A companion app for the GSPro golf simulator. Turns GSPro's `currentRound.dat` into one
self-contained HTML page: a 3D replay of the round, hole by hole, with a player switcher,
scorecard and shot data. Each page is complete: open it straight from disk or serve it from a
Cloud Storage bucket.

## Files
- `make_viewer.py`: the one command to run (see below)
- `build_round.py`: parses the round and course data, builds the ground (lidar or placeholder)
- `align_lidar.py`: lines lidar elevation up with GSPro's coordinates (run for you by the build)
- `fetch_osm.py`: downloads a course's OpenStreetMap features (fairways, greens, bunkers, paths...)
- `osm_surfaces.py`: turns those into surfaces in GSPro's coordinates (run for you by the build)
- `lidar_trees.py`: finds the real trees in the lidar point cloud
- `viewer.template.html`: the page; the round data is inlined into it

Round files, generated pages and the `lidar/` and `osm/` course data aren't in this repository.
The commands below create the course data; those folders are where the examples put it. The
examples use three real courses: Manele, Hobble Creek (USGS UT Central QL1 2018 survey) and York
Golf Club, Columbus, Ohio (USGS Columbus 2019 survey).

## Run
    pip install numpy scipy pillow rasterio shapely pyproj "laspy[lazrs]"
    python make_viewer.py --round path/to/currentRound.dat --course path/to/GSPro/Courses/<Course_GSP>

    # Manele with real ground and OpenStreetMap surfaces
    python make_viewer.py --round path/to/currentRound.dat --course path/to/GSPro/Courses/Manele_Golf_Course_GSP \
        --lidar lidar/manele_lidar.tif --align lidar/manele_align.json \
        --osm osm/manele_golf.json --osm osm/manele_cover.json --trees lidar/manele_trees.json

    # a new real-world course: fetch its OpenStreetMap data first
    python fetch_osm.py --course path/to/GSPro/Courses/<Course_GSP> --out osm/<name>

    # ...and its trees (Manele's survey; other places have their own point-cloud index)
    python lidar_trees.py --dem lidar/manele_lidar.tif --align lidar/manele_align.json \
        --ept https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/entwine/geoid12b/10331 \
        --osm osm/manele_cover.json --osm-shift osm/manele_shift.json --out lidar/manele_trees.json

The date on the page is the day GSPro last wrote `currentRound.dat` (the final shot), in the
local time of the machine running the script. Pass `--date YYYY-MM-DD` to set it yourself.

## Highlights
The Highlights button at the top (or H) plays the round's best moments in order, picked from the round
when the page loads: aces, eagles, hole-outs and bomb drives first, then stuck-it approaches and birdies
(the approach, then the putt rolling in) and the round's longest drive (the longest that finished in
the fairway; if it was a bomb, the bomb's card says so), then long putts holed. In rounds with more than
one player, the card leads with the player's name in their colour and the replayed shot is drawn in it. The
best shots play from several angles, cut together under a title card: down the line, a chase cam behind
bomb drives, a slow-motion landing view, and a slow-motion view from the green. Pressing anything else
(or the button again, or Esc) stops it.

The sun/moon button on the hole card switches between light and dark; the choice is remembered in
that browser. Otherwise the page follows the system setting.

## Celebrations in the replay
- A tee shot that carries 240+ yd explodes off the tee and flies as a fireball.
- An approach from 50+ yd that finishes inside 10 ft (the target grows to exactly 10 ft), or any shot holed from off the green.
- Birdie, eagle, albatross and hole in one each fly something out of the cup.
  Press 1-4 on a hole to preview them.

## A course with no course folder (Hobble Creek)
Everything also works from the round file alone. Find the course's location (OpenStreetMap knows most
real courses), then:

    # elevation model from the survey's own ground points (USGS surveys: find the one covering the
    # course in https://github.com/hobuinc/usgs-lidar boundaries/resources.geojson)
    python lidar_trees.py --make-dem lidar/hobblecreek_lidar.tif --lat 40.16083 --lon -111.51211 \
        --ept https://usgs-lidar-public.s3.us-west-2.amazonaws.com/USGS_LPC_UT_Central_QL1_B2_2018_LAS_2019
    python fetch_osm.py --lat 40.16083 --lon -111.51211 --out osm/hobblecreek
    python make_viewer.py --round currentRoundHC.dat --lidar lidar/hobblecreek_lidar.tif \
        --align lidar/hobblecreek_align.json --osm osm/hobblecreek_golf.json --osm osm/hobblecreek_cover.json
    python lidar_trees.py --dem lidar/hobblecreek_lidar.tif --align lidar/hobblecreek_align.json \
        --ept https://usgs-lidar-public.s3.us-west-2.amazonaws.com/USGS_LPC_UT_Central_QL1_B2_2018_LAS_2019 \
        --osm osm/hobblecreek_cover.json --osm-shift osm/hobblecreek_shift.json --out lidar/hobblecreek_trees.json
    # and once more with the trees
    python make_viewer.py ... --trees lidar/hobblecreek_trees.json

Without a course file the course name comes from OpenStreetMap, the pins are worked out from each shot's distance to the pin, the alignment uses
the ball positions alone, and there are no tee-shot images. USGS stores its surveys in web-map
coordinates; the scripts reproject them to UTM metres (needs pyproj).

## Real ground from lidar
Free real-world courses are built from public lidar surveys, so the survey lines up with GSPro's
course exactly once its position is found. `align_lidar.py` does that from points where GSPro's
ground height is known (ball rests, roll paths, tees, pins, hazard and boundary outlines): a brute
search over every placement and axis orientation, then a least-squares fit. For Manele, GSPro's x
runs east and z north, there is no rotation or scaling, and GSPro height = lidar elevation + 0.78 m.
Ball rests land within 2 cm of the lidar ground (median), pins within 3 cm.

Manele's lidar is NOAA/USGS 2022-23 (NOAA Digital Coast dataset 10335, 0.5 m, public domain),
cropped to the course and rounded to 1 cm. With no `--align` file the build solves the alignment
and saves it next to the lidar file. Fictional courses have no lidar and keep the fitted ground.

## Fairways, greens and bunkers from OpenStreetMap
Golf mappers trace fairways, greens, tees, bunkers, rough, water and cart paths in OpenStreetMap.
`osm_surfaces.py` moves those shapes into GSPro's coordinates through the lidar alignment, then finds
the small shift that puts every pin inside a green and every ball lie on the matching surface (the
imagery mappers trace from is often a few metres off; for Manele it is 8.75 m east, 3.5 m south).
At Manele, with that shift, all 72 pin positions sit inside OSM greens, every green and bunker lie
lands in an OSM green or bunker, and 85% of fairway lies land in an OSM fairway (the rest within a
few metres of the edge). The shapes are drawn into a 1 m-per-pixel surface texture over the
low-poly ground; fairways get mowing stripes along their length. Holes OSM doesn't cover keep the
approximated shapes. Map data (c) OpenStreetMap contributors (ODbL); the page credits it.

## Real trees from the lidar point cloud
The same survey publishes its raw points as a streamable index, so `lidar_trees.py` reads just the
course area (about 18 million points, roughly 30 seconds). The survey only labels ground, so canopy
height is everything else above the highest ground within a couple of metres (that keeps cliff faces
from passing for trees); OSM building footprints are masked out. Each treetop is a local high point of
the canopy and its crown is the patch of canopy around it, giving every tree a real position, height
and crown radius. Tall, narrow trees are drawn as columns and the rest as spreading crowns at their
measured width, which is a guess at shape, not a species map. Manele comes out at about 10,000 trees,
mostly low kiawe. The point cloud's heights are on a different reference from the elevation model
(23.5 m apart at Manele); the script measures that gap from ground points and removes it.

## What is real and what is placeholder
- Real, from `currentRound.dat`: every shot's flight and roll path, lies, scores, putts,
  penalties, mulligans and launch data, for every player in the round.
- Real, from the course `.GKD` (plain JSON): tee boxes, the pin set played (matched
  automatically), aim points, penalty-area outlines and the out-of-bounds line, card
  yardages, stroke index and rating.
- Real with `--lidar`: the ground, coastline and sea. Without it the terrain is fitted to every
  recorded ball height, since the `.gspcrse` course package is encrypted.
- Real with `--osm`: fairway, green, tee, bunker, water, path and building shapes.
- Real with `--trees`: tree positions, heights and crown sizes (shapes are approximate).
- Placeholder: trees without `--trees`; surface shapes away from recorded lies without `--osm`.
- Club names above the driver are inferred from GSPro's club numbers.

## Notes on GSPro's round file
- `ShotResult` 3 (seen at Hobble Creek): out of bounds / lost ball. The row ends back where the shot
  was played from (stroke and distance); the page shows it as "OB · re-hit".
- One JSON row per stroke. Coordinates are Unity world metres, the same frame as the `.GKD`.
- `ShotResult`: 0 normal, 1 mulligan (replayed, not counted), 2 penalty (row ends at the drop).
- `HoleResult`: 7 holed, 3 in play, 2 tee shot finished on a par-3 green.
- Rows that repeat the last shot's `GlobalShotNumber` are auto-putts (no putt path).
- Club 26 with no flight points is a real putt (roll path only).
- `DistanceToPin` is metres from where the ball came to rest after the shot.
