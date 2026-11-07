# Retrace

Relive your GSPro rounds. Retrace runs quietly on your GSPro PC, and when you finish a round it builds a
3D replay you can open in any browser: every shot's flight and roll, hole by hole, with a scorecard, shot
data, a player switcher and a highlight reel of the round's best moments. Your rounds stay on your own PC, listed
on one page that opens in your browser. If you'd like to see them from anywhere, you can also put them in a cloud bucket.

Retrace is an independent community project, not made by or affiliated with GSPro. It's an early prototype,
so please open an issue if something doesn't work on your setup.

## Install

On the PC that runs GSPro:

1. On this project's [GitHub page](https://github.com/digitaljoel/retrace), click **Code > Download ZIP**.
2. Right-click the zip and choose **Extract All**.
3. Open the extracted folder and double-click **Install.bat**.

That's it. You don't need to install Python or have administrator rights, and the zip can be deleted afterwards.
Retrace takes about 400 MB, and setup needs an internet connection.

If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**. Retrace isn't code-signed
yet, so Windows doesn't recognize it. You can read `install.ps1` first to see exactly what it does.

**Then play a round.** Retrace runs in the background and starts whenever you sign in. Shortly after you hole out
on the 18th, double-click **Retrace Rounds** on your desktop to see your rounds in your browser.

To update, download the new zip and run **Install.bat** again; your rounds and settings stay. To remove Retrace, run
**Uninstall.bat** in `%LOCALAPPDATA%\Programs\Retrace`. Your rounds stay in `C:\Users\<you>\Retrace` until you delete them.

Setup finds your GSPro courses on its own, even if GSPro isn't in the usual place. If it can't, it asks you for the
folder; you can also skip that and see the next section.

## Where Retrace looks for GSPro

| What | Where it looks | To change it |
|---|---|---|
| The round file | `C:\Users\<you>\AppData\LocalLow\GSPro\GSPro\currentRound.dat`, then any `currentRound.dat` elsewhere in `LocalLow` | `retrace.py set round_file "D:/path/to/currentRound.dat"` |
| GSPro's courses | `C:\GSProV1\Core\GSP\Courses`, then the same path on your other hard drives, then the folders named in GSPro's log files, then a search of your hard drives lasting up to 30 seconds | `retrace.py find-courses "D:/path/to/Courses"` |
| Where pages go | `C:\Users\<you>\Retrace\rounds` | `retrace.py set output_dir "D:/path/to/Retrace"` |

`AppData` is a hidden folder. To find it, paste `%USERPROFILE%\AppData\LocalLow` into File Explorer's address
bar. If you moved your courses, the location is in GSPro under **Settings > Game > Course Folder**.

These are commands; see [Commands](#commands) for how to run them. After changing a setting, restart your PC or the watcher.

## How it knows a round is over

GSPro's round file doesn't say when a round has ended, so Retrace works it out. It builds the page when:

- all 18 holes are played, every player has holed out, and the file hasn't changed for **1 minute**;
- at least **9 holes** are played and the file hasn't changed for **30 minutes** (nine-hole rounds, and rounds
  you stopped early); or
- at least 9 holes are played and GSPro starts a new round.

GSPro starts the round file over with each new round, so Retrace saves a copy after every change, in
`C:\Users\<you>\Retrace\saved`. Going straight into another round can't lose the last one. If you pause for
more than 30 minutes mid-round, you'll get a page marked "thru 12", say, and it's rebuilt when you finish.

Rounds under 9 holes are saved but not built. To build one anyway, or to change the timings:

```powershell
.venv\Scripts\python.exe retrace.py build 131     # a saved round's number, or its file
.venv\Scripts\python.exe retrace.py set min_holes 6
.venv\Scripts\python.exe retrace.py set idle_minutes 60
.venv\Scripts\python.exe retrace.py set quiet_seconds 120
```

Builds run at low priority, so they shouldn't get in the way if you start another round right away.

## Optional: see your rounds from anywhere

This is an extra, and nothing is uploaded unless you set it up. Retrace can copy each new page and the rounds list to
a Google Cloud Storage or Amazon S3 bucket. Hosting pages this small costs little, usually pennies a month, but both
providers need an account with billing, and setup is more technical than the rest of Retrace.

> **A public bucket is public.** Anyone with the link can see the pages, which include the names and scores
> of everyone in the round. Ask your playing partners first. The pages also include tee-shot pictures from
> the course's files where they exist, so check you're happy to share those. Your saved round files, logs and
> settings are never uploaded.

Retrace uploads with your own `gcloud` or `aws` command-line tool, or, for Google Cloud, with a service account
key that you put in its folder (see below). Bucket names are shared worldwide, so pick your own in place of
`my-golf-replays-example`.

### Google Cloud Storage

Install the [gcloud CLI](https://cloud.google.com/sdk/docs/install), run `gcloud auth login`, then:

```powershell
gcloud storage buckets create gs://my-golf-replays-example --location=us-central1 --uniform-bucket-level-access --public-access-prevention
.venv\Scripts\python.exe retrace.py set publish_to gs://my-golf-replays-example
.venv\Scripts\python.exe retrace.py publish
```

That bucket is private. You can look through it in the Google Cloud console. When you're ready to share it:

```powershell
gcloud storage buckets update gs://my-golf-replays-example --no-public-access-prevention
gcloud storage buckets add-iam-policy-binding gs://my-golf-replays-example --member=allUsers --role=roles/storage.objectViewer
```

Your rounds are then at `https://storage.googleapis.com/my-golf-replays-example/index.html`. To make it private
again, remove that access with `remove-iam-policy-binding` and the same two options, then run the first
command with `--public-access-prevention`.

**Uploading without gcloud.** A `gcloud auth login` sign-in expires, and the GSPro PC doesn't need gcloud at all if
you give Retrace a service account key instead. You can make it from any PC, in the Google Cloud console:

1. **IAM & Admin > Service Accounts > Create service account.** Name it, say, `retrace-uploader`, and skip the
   optional role steps.
2. **Cloud Storage > Buckets >** your bucket **> Permissions > Grant access.** Paste the service account's email
   and pick **Storage Object Admin** (Retrace replaces pages, which Storage Object Creator can't do).
3. Open the service account, then **Keys > Add key > Create new key > JSON.**
4. Copy the downloaded file to the GSPro PC as `%LOCALAPPDATA%\Programs\Retrace\gcp-key.json`, next to
   `retrace.py`.

That's all: Retrace uses `gcp-key.json` whenever it's there, and the key doesn't expire. `retrace.py check` says
"with gcp-key.json" under Publishing when the key works. Treat the file like a password: it can write to your
bucket, so don't email it or put it in a repository, and delete the copy in your Downloads folder. If it's ever
exposed, delete that key in the console and make a new one. Uninstalling removes it along with the rest of the
install folder.

### Amazon S3

Install the [AWS CLI](https://aws.amazon.com/cli/), run `aws configure`, then:

```powershell
aws s3 mb s3://my-golf-replays-example
.venv\Scripts\python.exe retrace.py set publish_to s3://my-golf-replays-example
.venv\Scripts\python.exe retrace.py publish
```

New S3 buckets are private. To share it, save this as `policy.json` with your bucket name in it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": "arn:aws:s3:::my-golf-replays-example/*"
  }]
}
```

```powershell
aws s3api delete-public-access-block --bucket my-golf-replays-example
aws s3api put-bucket-policy --bucket my-golf-replays-example --policy file://policy.json
```

Your rounds are then at `https://my-golf-replays-example.s3.<region>.amazonaws.com/index.html`, where `<region>` is
your bucket's region.

### After that

Restart your PC or the watcher, and new rounds upload themselves. If an upload fails, for example because the internet
is down, it tries again every 5 minutes. To put rounds in a folder, use `gs://my-golf-replays-example/golf`. Retrace
only adds and updates files: a page you delete from your PC stays in the bucket until you delete it there too.

## Commands

Most people won't need these. Open PowerShell and go to the Retrace folder, then start each with `.venv\Scripts\python.exe`:

```powershell
cd "$env:LOCALAPPDATA\Programs\Retrace"
.venv\Scripts\python.exe retrace.py check
```

To restart the watcher: `.venv\Scripts\python.exe retrace.py stop`, then `.venv\Scripts\pythonw.exe retrace.py watch`.

| Command | What it does |
|---|---|
| `retrace.py check` | Shows what was found and what's missing |
| `retrace.py watch` | Runs the watcher in this window, showing what it's doing (Ctrl+C to stop) |
| `retrace.py open` | Shows your rounds in your browser |
| `retrace.py stop` | Stops the background watcher |
| `retrace.py startup on` / `off` | Starts the watcher when you sign in, or doesn't |
| `retrace.py build [ROUND] [--date YYYY-MM-DD]` | Builds one round now, finished or not: a saved round's number or file (default: the current round) |
| `retrace.py index` | Rebuilds the rounds list from the pages in the folder |
| `retrace.py publish` | Uploads anything new or changed |
| `retrace.py merge ROUND ROUND` | Joins two saved rounds into one page, such as when a restart made GSPro split a round in two |
| `retrace.py find-courses [FOLDER]` | Finds GSPro's courses if Retrace doesn't know where they are, or looks in the folder you give it, and remembers the result |
| `retrace.py set SETTING VALUE` | Changes a setting; settings are kept in `config.json` |

## Troubleshooting

**Nothing appears after a round.** Run `retrace.py check` and confirm the watcher is running and the round
file is found. Then read `C:\Users\<you>\Retrace\retrace.log`, which records every round seen, page built,
upload and error.

**The round file isn't found, but you've played.** Search `LocalLow` for `currentRound.dat` and give Retrace
the path with `set round_file`.

**A round got split in two.** If your PC restarts mid-round, GSPro can carry on under a new round number. Join the
parts with `retrace.py merge 126 127`, using the numbers from `check` or the file names in `saved`. If a hole is in
both parts, the one that finished it is used.

**The page has few course details.** "No .GKD file" in `check` means Retrace couldn't find that course's folder,
so the page is built from shot data alone. Check `courses_dir`, then rebuild the round from `saved`.

**A build failed.** The reason is in `retrace.log`. A failed round is tried again if it changes or the watcher
restarts. Please include the error if you open an issue, but look it over first and leave out anything private.

**Setup failed.** The window says why. Check your internet connection and run **Install.bat** again; it's safe to
run more than once.

## Real terrain, greens and trees

You don't need to set anything up. The first time you play a real-world course, Retrace reads the course's
location from its course file and downloads what it needs, once per course. Later rounds there reuse it and build
in seconds. In the US that means reading about 2 GB of lidar over 5 to 10 minutes, so keep it in mind if your
internet has a data cap, though only about 30 MB is kept. Elsewhere the download is much smaller.

The map and the ground start downloading as soon as a round on a new course begins, at Windows' lowest priority
for the processor, disk and memory, so the game comes first and less is left to do when you finish. The trees
still wait for the end of the round. To wait for the end for everything: `retrace.py set download_early off`.

The page shows everything inside the course's outer edge, from its OpenStreetMap outline and the course file's
out-of-bounds line, plus about 100 m around the golf: your shots wherever they went, the tees, pins and hazards,
and the mapped fairways, greens, tees and bunkers. Neighbourhoods inside the course stay; ones beyond its edge are
left out.

| | In the United States | Everywhere else |
|---|---|---|
| **Ground** | Public USGS lidar, accurate to centimetres | 5 m worldwide terrain, fitted to your ball heights. It's right where you played, but rougher away from your shots |
| **Trees** | Found in the lidar | Meta and WRI's worldwide 1 m canopy height map, which finds fewer trees |
| **Fairways, greens, bunkers, water** | OpenStreetMap | OpenStreetMap |

Places USGS doesn't cover, even within the US, use the worldwide sources. Surface shapes are only as good as the
course's mapping in OpenStreetMap, so holes nobody has traced keep approximate shapes.

Retrace checks the terrain against where your balls actually landed before using it. If it doesn't fit, or the
course is fictional and has no location, you still get a full replay, with placeholder ground and trees. If a
download fails, for example with no internet, that round's page gets whatever worked, and the next round on the
course tries again. Everything is kept in `C:\Users\<you>\Retrace\course-data`; delete a course's folder there to
download it fresh.

If a course file has no location, you can give it one, using the course key `check` shows:

```powershell
.venv\Scripts\python.exe retrace.py set location.Some_Course_GSP "40.1608,-111.5121"
```

Credits: map data (c) OpenStreetMap contributors (ODbL); lidar from the USGS 3D Elevation Program; terrain tiles
from Mapzen and the AWS Terrain Tiles dataset; canopy heights from Meta and the World Resources Institute (CC BY 4.0).
The pages credit these too.

## How it works

### Files
- `Install.bat`, `install.ps1`, `Uninstall.bat`: setup and removal
- `retrace.py`: the watcher and the commands above
- `make_viewer.py`: builds one page; `retrace.py` calls it, and you can run it yourself
- `make_index.py`: builds the rounds list from the pages in a folder
- `build_round.py`: parses the round and course data, builds the ground (lidar or placeholder)
- `align_lidar.py`: lines lidar elevation up with GSPro's coordinates (run for you by the build)
- `fetch_osm.py`: downloads a course's OpenStreetMap features (fairways, greens, bunkers, paths...)
- `osm_surfaces.py`: turns those into surfaces in GSPro's coordinates (run for you by the build)
- `lidar_trees.py`: finds the real trees in the lidar point cloud
- `course_data.py`: downloads a course's map, elevation and trees, and solves its alignment
- `viewer.template.html`: the page; the round data is inlined into it

Each page is complete: open it straight from disk or serve it from a bucket. The date on the page is the day
GSPro last wrote the round file (the final shot), in the local time of the PC building it.

To run from source instead of using the installer, with Python 3.9 or newer: `python -m venv .venv`, then
`.venv\Scripts\python.exe -m pip install -r requirements.txt`. `requirements.lock` holds the exact versions the installer uses.

To build a page by hand with `make_viewer.py`:

    python make_viewer.py --round path/to/currentRound.dat --course path/to/GSPro/Courses/<Course_GSP> \
        [--lidar lidar/x_lidar.tif --align lidar/x_align.json] \
        [--osm osm/x_golf.json --osm osm/x_cover.json] [--trees lidar/x_trees.json]

### Highlights
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

### Celebrations in the replay
- A tee shot that carries 240+ yd explodes off the tee and flies as a fireball.
- An approach from 50+ yd that finishes inside 10 ft (the target grows to exactly 10 ft), or any shot holed from off the green.
- Birdie, eagle, albatross and hole in one each fly something out of the cup.
  Press 1-4 on a hole to preview them.

### A course with no course folder
Everything also works from the round file alone, given the course's location (see `location.<key>` above). The course name comes from OpenStreetMap, the pins are worked
out from each shot's distance to the pin, the alignment uses the ball positions alone, and there are no tee-shot
images. USGS stores its surveys in web-map coordinates; the scripts reproject them to UTM metres.

### Real ground from lidar
Free real-world courses are built from public lidar surveys, so the survey lines up with GSPro's
course exactly once its position is found. `align_lidar.py` does that from points where GSPro's
ground height is known (ball rests, roll paths, tees, pins, hazard and boundary outlines): a brute
search over every placement and axis orientation, then a least-squares fit. For Manele, GSPro's x
runs east and z north, there is no rotation or scaling, and GSPro height = lidar elevation + 0.78 m.
Ball rests land within 2 cm of the lidar ground (median), pins within 3 cm. Fictional courses have no
lidar and keep the fitted ground.

### Fairways, greens and bunkers from OpenStreetMap
Golf mappers trace fairways, greens, tees, bunkers, rough, water and cart paths in OpenStreetMap.
`osm_surfaces.py` moves those shapes into GSPro's coordinates through the lidar alignment, then finds
the small shift that puts every pin inside a green and every ball lie on the matching surface (the
imagery mappers trace from is often a few metres off; for Manele it is 8.75 m east, 3.5 m south).
At Manele, with that shift, all 72 pin positions sit inside OSM greens, every green and bunker lie
lands in an OSM green or bunker, and 85% of fairway lies land in an OSM fairway (the rest within a
few metres of the edge). The shapes are drawn into a 1 m-per-pixel surface texture over the
low-poly ground; fairways get mowing stripes along their length. Holes OSM doesn't cover keep the
approximated shapes.

### Real trees from the lidar point cloud
The same survey publishes its raw points as a streamable index, so `lidar_trees.py` reads just the
course area (about 18 million points, roughly 30 seconds). The survey only labels ground, so canopy
height is everything else above the highest ground within a couple of metres (that keeps cliff faces
from passing for trees); OSM building footprints are masked out. Each treetop is a local high point of
the canopy and its crown is the patch of canopy around it, giving every tree a real position, height
and crown radius. Tall, narrow trees are drawn as columns and the rest as spreading crowns at their
measured width, which is a guess at shape, not a species map. Manele comes out at about 10,000 trees,
mostly low kiawe. The point cloud's heights are on a different reference from the elevation model
(23.5 m apart at Manele); the script measures that gap from ground points and removes it.

### What is real and what is placeholder
- Real, from `currentRound.dat`: every shot's flight and roll path, lies, scores, putts,
  penalties, mulligans and launch data, for every player in the round.
- Real, from the course `.GKD` (plain JSON): tee boxes, the pin set played (matched
  automatically), aim points, penalty-area outlines and the out-of-bounds line, card
  yardages, stroke index and rating.
- Real with lidar: the ground, coastline and sea. Without it the terrain is fitted to every
  recorded ball height, since the `.gspcrse` course package is encrypted.
- Real with OpenStreetMap: fairway, green, tee, bunker, water, path and building shapes.
- Real with lidar trees: tree positions, heights and crown sizes (shapes are approximate).
- Placeholder: trees without lidar trees; surface shapes away from recorded lies without OpenStreetMap.
- Club names above the driver are inferred from GSPro's club numbers.

### Notes on GSPro's round file
- One JSON row per stroke. Coordinates are Unity world metres, the same frame as the `.GKD`.
- `ShotResult`: 0 normal, 1 mulligan (replayed, not counted), 2 penalty (row ends at the drop),
  3 out of bounds / lost ball (the row ends back where the shot was played from, stroke and distance;
  the page shows it as "OB · re-hit").
- `HoleResult`: 7 holed, 3 in play, 2 tee shot finished on a par-3 green.
- Rows that repeat the last shot's `GlobalShotNumber` are auto-putts (no putt path).
- Club 26 with no flight points is a real putt (roll path only).
- `DistanceToPin` is metres from where the ball came to rest after the shot.
