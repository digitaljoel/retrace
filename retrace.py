"""Retrace: watch GSPro's round file and turn each finished round into a 3D replay page.

    python retrace.py check                  show what was found and what's missing
    python retrace.py watch                  keep running; build each round when it finishes
    python retrace.py build [FILE]           build one round now, finished or not
    python retrace.py open                   show your rounds in your web browser
    python retrace.py index                  rebuild the rounds list (index.html)
    python retrace.py publish                upload new and changed pages to the bucket
    python retrace.py set KEY VALUE          change a setting (see the README)
    python retrace.py merge ROUND ROUND      join a round that GSPro split in two
    python retrace.py find-courses [FOLDER]  find GSPro's courses, on this PC or in that folder
    python retrace.py startup on|off         start the watcher when you sign in to Windows
    python retrace.py stop                   stop a running watcher

GSPro's round file has no "round finished" marker, so a round counts as finished when:
  - 18 holes are played and every player has holed out on all of them, and the file has been
    quiet for quiet_seconds; or
  - at least min_holes holes are played and the file has been quiet for idle_minutes (nine-hole
    rounds, and rounds stopped early); or
  - at least min_holes holes are played and GSPro has moved on to a new round.
Each version of the round file is copied to the saved folder first, so a new round overwriting
currentRound.dat can't lose the previous one. A round that continues later is simply rebuilt.
"""
import argparse, hashlib, json, logging, logging.handlers, os, re, shutil, string, subprocess, sys, time
from pathlib import Path

import make_index

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
DEFAULTS = {
    "round_file": "",       # empty: look in the usual place
    "courses_dir": "",      # empty: look in the usual place
    "output_dir": "",       # empty: <home>/Retrace
    "publish_to": "",       # gs://bucket[/folder] or s3://bucket[/folder]
    "quiet_seconds": 60,
    "idle_minutes": 30,
    "min_holes": 9,
    "download_early": True, # get a course's map and elevation while its round is played, at the lowest priority
    "locations": {},        # course key -> "lat,lon", for a course whose file doesn't say where it is
}
POLL_SECONDS = 5
EARLY = {}  # course key -> the early download started for it in this watcher (see start_early_download)
PUBLISH_RETRY_SECONDS = 300
log = logging.getLogger("retrace")


# ---------------------------------------------------------------- settings and paths
def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8-sig")))
    return cfg


def round_file(cfg):
    """The configured round file, or GSPro's usual location, or any match under AppData/LocalLow."""
    if cfg["round_file"]:
        return Path(cfg["round_file"])
    low = Path.home() / "AppData" / "LocalLow"
    usual = low / "GSPro" / "GSPro" / "currentRound.dat"
    if usual.exists():
        return usual
    found = sorted(low.glob("*/*/currentRound.dat")) if low.is_dir() else []
    return found[0] if found else usual


def run_me(*args):
    """Commands to show people, which work in both cmd.exe and PowerShell. "python" usually isn't on their PATH,
    because the installer keeps its Python private, and a quoted program path would need "&" in PowerShell only."""
    exe = Path(sys.executable)
    python = exe.relative_to(HERE) if exe.is_relative_to(HERE) else exe
    return f'cd "{HERE}"\n       ' + " ".join([str(python), "retrace.py", *args])


def courses_dir(cfg):
    if cfg["courses_dir"]:
        return Path(cfg["courses_dir"])
    for drive in "CDEFGH":
        p = Path(f"{drive}:/GSProV1/Core/GSP/Courses")
        if p.is_dir():
            return p
    return Path("C:/GSProV1/Core/GSP/Courses")


# ---------------------------------------------------------------- finding GSPro's courses, wherever they are
RESTART_NOTE = "Retrace is running in the background and will use this after it restarts: run Install.bat again, or sign out and back in."
SEARCH_SECONDS = 30
SEARCH_DEPTH = 6        # deep enough for D:\Games\Golf\GSPro\Core\GSP\Courses
SKIP_DIRS = {"windows", "appdata", "programdata", "node_modules", "system volume information", "recovery", "perflogs"}


def course_count(folder):
    """How many courses a folder holds: its subfolders with a .GKD file in them."""
    count = 0
    try:
        for entry in os.scandir(folder):
            if entry.is_dir() and any(True for _ in Path(entry.path).glob("*.[gG][kK][dD]")):
                count += 1
    except OSError:
        pass
    return count


def fixed_drives():
    """Local hard drives only: asking a network or removable drive can hang, or ask for a disc."""
    try:
        import ctypes
        kernel = ctypes.windll.kernel32
        return [f"{d}:\\" for d in string.ascii_uppercase if kernel.GetDriveTypeW(f"{d}:\\") == 3]
    except (AttributeError, OSError):
        return [f"{d}:\\" for d in "CDEFGH" if Path(f"{d}:\\").is_dir()]


def courses_from_path(text):
    """The Courses folder implied by a path that GSPro wrote down, if it implies one."""
    parts = [p for p in re.split(r"[\\/]+", text) if p]
    for i, part in enumerate(parts):
        if part.lower() == "core" and [p.lower() for p in parts[i + 1:i + 2]] == ["gsp"]:
            return Path(parts[0] + "\\", *parts[1:i], "Core", "GSP", "Courses")
        if "courses" in part.lower():
            return Path(parts[0] + "\\", *parts[1:i + 1])
    return None


def courses_from_gspro_files(cfg):
    """Unity games log where they run from, and GSPro keeps its logs and settings next to the round file."""
    found = []
    try:
        files = list(round_file(cfg).parent.iterdir())
    except OSError:
        return found
    for f in files:
        try:
            if f.suffix.lower() not in (".log", ".txt", ".json", ".cfg", ".ini", ".xml") or f.stat().st_size > 5_000_000:
                continue
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for path in re.findall(r"[A-Za-z]:[\\/]+[^\"'<>|\r\n*?]+", text):
            p = courses_from_path(path)
            if p is not None and p not in found:
                found.append(p)
    return found


def walk_for_courses(roots):
    """Yields likely Courses folders under the roots, shallowest first, until time or depth runs out."""
    deadline = time.monotonic() + SEARCH_SECONDS
    level = [Path(r) for r in roots]
    for _ in range(SEARCH_DEPTH + 1):
        below = []
        for folder in level:
            if time.monotonic() > deadline:
                log.info("stopped searching for courses after %d seconds", SEARCH_SECONDS)
                return
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False) or e.name.startswith(("$", ".")) or e.name.lower() in SKIP_DIRS:
                        continue
                except OSError:
                    continue
                if "course" in e.name.lower() and course_count(e.path):
                    yield Path(e.path)      # the courses are inside, so there's no need to look further down
                else:
                    below.append(e.path)
        level = below


def find_courses(cfg, within=None):
    """The best Courses folder: a look in the likely places first, then a time-limited search of the hard drives.
    If there are several (a backup, say), the one holding the last course played wins, then the one with most courses."""
    played = None
    try:
        rows = parse_rounds_file(round_file(cfg).read_bytes())
        played = summarize(rows)["course"] if rows else None
    except (OSError, ValueError, KeyError, TypeError):
        pass

    def best(candidates):
        scored = [(course_count(c), c) for c in candidates]
        scored = [(bool(played and (c / str(played)).is_dir()), n, c) for n, c in scored if n]
        return max(scored, key=lambda s: s[:2])[2] if scored else None

    if within is not None:
        within = Path(within)
        return best([within, within / "Courses", within / "Core" / "GSP" / "Courses", *walk_for_courses([within])])
    usual = [Path(f"{d}/GSProV1/Core/GSP/Courses") for d in fixed_drives()]
    return best(usual + courses_from_gspro_files(cfg)) or best(walk_for_courses(fixed_drives()))


def save_setting(key, value):
    raw = load_config_file()
    raw[key] = value
    write_atomic(CONFIG, json.dumps(raw, indent=2).encode("utf-8"))


def cmd_find_courses(cfg, a):
    """Exit codes the installer relies on: 0 the folder is known, 1 it isn't."""
    if not a.folder and courses_dir(cfg).is_dir():
        print("Courses folder:", courses_dir(cfg))
        return 0
    found = find_courses(cfg, a.folder)
    if found is None:
        print(f"No courses found in {a.folder}" if a.folder else "Couldn't find GSPro's courses on this PC's hard drives.")
        return 1
    save_setting("courses_dir", found.as_posix())
    print(f"Found courses in {found} ({course_count(found)})")
    if watcher_running(cfg):
        print(RESTART_NOTE)
    return 0


def output_dir(cfg):
    return Path(cfg["output_dir"]) if cfg["output_dir"] else Path.home() / "Retrace"


def rounds_dir(cfg):
    return output_dir(cfg) / "rounds"


def stem_for(course_key, round_id):
    name = re.sub(r"_GSP$", "", str(course_key), flags=re.I).lower()
    return f"{re.sub(r'[^a-z0-9_-]+', '-', name)}-round-{round_id}"


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def load_state(cfg):
    p = output_dir(cfg) / "state.json"
    state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    state.setdefault("rounds", {})
    state.setdefault("published", {})
    return state


def save_state(cfg, state):
    write_atomic(output_dir(cfg) / "state.json", json.dumps(state, indent=1).encode("utf-8"))


# ---------------------------------------------------------------- reading a round
def parse_rounds_file(raw):
    """The rows in a round file, or None if it's empty or only partly written."""
    try:
        rows = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[-1], dict) or "RoundID" not in rows[-1]:
        return None
    return rows


def summarize(rows):
    holes = {r["Hole"] for r in rows}
    last = {}
    for r in rows:
        last[r["UserGuid"], r["Hole"]] = r["HoleResult"]
    players = {r["UserGuid"] for r in rows}
    all_holed = all(last.get((p, h)) == 7 for p in players for h in holes)
    return {"round_id": rows[-1]["RoundID"], "course": rows[-1]["CourseKey"], "holes": len(holes), "all_holed": all_holed}


def is_due(rec, is_current, now, cfg):
    """Whether this saved round should be built now."""
    if rec.get("built_sha") == rec["sha"] or rec["holes"] < cfg["min_holes"]:
        return False
    if not is_current:
        return True  # GSPro has moved on, so this round won't change
    finished = rec["holes"] >= 18 and rec["all_holed"]
    wait = cfg["quiet_seconds"] if finished else cfg["idle_minutes"] * 60
    return now - rec["mtime"] >= wait


# ---------------------------------------------------------------- building a page
def gkd_file(cfg, course_key):
    """The course's .GKD file, if its folder is here."""
    course = courses_dir(cfg) / str(course_key)
    found = [p for p in course.iterdir() if p.suffix.upper() == ".GKD"] if course.is_dir() else []
    return found[0] if found else None


def course_location(cfg, course_key):
    """(lat, lon) from the settings or the course file. Fictional courses have none, or 0, 0."""
    manual = cfg["locations"].get(course_key)
    if manual:
        lat, lon = str(manual).split(",")
        return float(lat), float(lon)
    gkd = gkd_file(cfg, course_key)
    if gkd:
        try:
            g = json.loads(gkd.read_text(encoding="utf-8-sig"))
            lat, lon = float(g["CourseLat"]), float(g["CourseLong"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if (lat or lon) and abs(lat) <= 90 and abs(lon) <= 180:
            return lat, lon
    return None


def course_data_dir(cfg, course_key):
    return output_dir(cfg) / "course-data" / re.sub(r"[^A-Za-z0-9_-]+", "-", str(course_key))


def prepare_course(cfg, source, course_key):
    """Download whatever this course's real terrain still needs. A failure only means a plainer page."""
    where = course_location(cfg, course_key)
    if not where:
        if gkd_file(cfg, course_key):
            log.info("%s: no location in its course file, so no real terrain", course_key)
        else:
            log.warning("%s: no course file in %s, so no location and no real terrain", course_key, courses_dir(cfg) / str(course_key))
        return
    wait_for_early_download(course_key)    # both would be downloading the same files
    gkd = gkd_file(cfg, course_key)
    args = [sys.executable, str(HERE / "course_data.py"), "--folder", str(course_data_dir(cfg, course_key)), "--lat", str(where[0]), "--lon", str(where[1]),
            "--round", str(source), "--gkd", str(gkd) if gkd else "-"]
    log.info("%s: getting course data for %.5f, %.5f", course_key, *where)
    try:
        done = run_quietly(args, timeout=3600)
    except subprocess.TimeoutExpired:
        log.error("%s: course data timed out", course_key)
        return
    if done.stdout.strip():
        log.info("%s", done.stdout.strip())
    if done.returncode:
        log.error("%s: course data failed\n%s", course_key, "\n".join(done.stderr.strip().splitlines()[-12:]))


def start_early_download(cfg, course_key):
    """Start getting a course's map and elevation while its round is played, so a finished round has less to wait for.
    course_data.py --download-only runs in Windows' background mode, so the game comes first."""
    if not cfg["download_early"] or course_key in EARLY:
        return
    EARLY[course_key] = None
    where, state = course_location(cfg, course_key), course_sources(cfg, course_key)
    if not where or (state.get("map") and state.get("elevation")):
        return  # nothing to download, or it was all downloaded before
    d = course_data_dir(cfg, course_key)
    d.mkdir(parents=True, exist_ok=True)
    args = [sys.executable, str(HERE / "course_data.py"), "--folder", str(d), "--lat", str(where[0]), "--lon", str(where[1]), "--download-only"]
    flags = subprocess.IDLE_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(d / "early-download.log", "w", encoding="utf-8") as out:
        EARLY[course_key] = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, cwd=str(HERE), creationflags=flags,
                                             env=dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8"))
    log.info("%s: downloading its map and elevation in the background while the round is played", course_key)


def wait_for_early_download(course_key):
    proc = EARLY.get(course_key)
    if proc is None or proc.poll() is not None:
        return
    log.info("%s: waiting for the background download to finish", course_key)
    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        log.error("%s: the background download timed out", course_key)


def course_sources(cfg, course_key):
    """What has been downloaded for this course so far (see course_data.py)."""
    try:
        return json.loads((course_data_dir(cfg, course_key) / "source.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def viewer_args(cfg, source, course_key, out, date=None):
    """make_viewer.py's arguments, using the course folder and whatever course data has been downloaded."""
    args = [sys.executable, str(HERE / "make_viewer.py"), "--round", str(source), "--out", str(out)]
    if date:
        args += ["--date", date]
    course = courses_dir(cfg) / str(course_key)
    if course.is_dir():
        args += ["--course", str(course)]
    state, d = course_sources(cfg, course_key), course_data_dir(cfg, course_key)
    if state.get("aligned"):
        args += ["--lidar", str(d / "elevation.tif"), "--align", str(d / "align.json"), "--terrain-credit", state["credit"]]
        if state.get("map"):
            args += ["--osm", str(d / "osm_golf.json"), "--osm", str(d / "osm_cover.json"), "--osm-shift", str(d / "shift.json")]
        if state.get("trees"):
            args += ["--trees", str(d / "trees.json"), "--trees-credit", state["trees"]]
        if state.get("buildings"):
            args += ["--buildings", str(d / "buildings.json")]
    return args


def run_quietly(args, timeout):
    """Run a command at low priority with no console window, so a build can't disturb the next round."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=flags, env=env, cwd=str(HERE))


def build_page(cfg, source, course_key, round_id, date=None):
    """Build one page and refresh the index. Returns the page's path, or None if the build failed."""
    out = rounds_dir(cfg) / f"{stem_for(course_key, round_id)}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    source = Path(source).resolve()
    prepare_course(cfg, source, course_key)
    try:
        done = run_quietly(viewer_args(cfg, source, course_key, out, date), timeout=1200)
    except subprocess.TimeoutExpired:
        log.error("build timed out: %s", out.name)
        return None
    if done.returncode != 0:
        log.error("build failed: %s\n%s", out.name, "\n".join(done.stderr.strip().splitlines()[-30:]))
        return None
    log.info("built %s", out)
    make_index.write_index(rounds_dir(cfg))
    return out


# ---------------------------------------------------------------- publishing
def upload_command(target, path):
    dest = f"{target.rstrip('/')}/{path.name}"
    if target.startswith("gs://"):
        tool = shutil.which("gcloud")
        if not tool:
            raise RuntimeError("the gcloud command isn't installed or isn't on PATH")
        return [tool, "storage", "cp", str(path), dest, "--cache-control=no-cache", "--content-type=text/html"]
    if target.startswith("s3://"):
        tool = shutil.which("aws")
        if not tool:
            raise RuntimeError("the aws command isn't installed or isn't on PATH")
        return [tool, "s3", "cp", str(path), dest, "--cache-control", "no-cache", "--content-type", "text/html",
                "--only-show-errors"]
    raise RuntimeError(f"publish_to must start with gs:// or s3://, not {target!r}")


def publish(cfg, state):
    """Upload new and changed pages, then the index. Returns False if anything failed."""
    target = cfg["publish_to"]
    if not target:
        return True
    folder = rounds_dir(cfg)
    if not folder.is_dir():
        return True
    # pages first, so the index never links to a page that isn't there yet
    files = sorted(p for p in folder.glob("*.html") if p.name != make_index.INDEX) + [folder / make_index.INDEX]
    for path in files:
        if not path.exists():
            continue
        st = path.stat()
        signature = f"{st.st_size}:{st.st_mtime_ns}"
        if state["published"].get(path.name) == signature:
            continue
        try:
            done = run_quietly(upload_command(target, path), timeout=900)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            log.error("upload failed: %s: %s", path.name, e)
            return False
        if done.returncode != 0:
            log.error("upload failed: %s\n%s", path.name, done.stderr.strip()[-800:])
            return False
        state["published"][path.name] = signature
        save_state(cfg, state)
        log.info("uploaded %s", path.name)
    return True


# ---------------------------------------------------------------- the watcher
class Watcher:
    def __init__(self, cfg):
        self.cfg, self.state = cfg, load_state(cfg)
        self.last_stat, self.failed = None, {}
        self.next_publish = 0.0  # checked once at start, and again after each build or failed upload

    def snapshot(self):
        """Copy the round file to the saved folder when its contents change."""
        path = round_file(self.cfg)
        try:
            st = path.stat()
            if self.last_stat == (st.st_mtime_ns, st.st_size):
                return
            raw = path.read_bytes()
        except OSError:
            return  # not there yet, or GSPro has it locked: try again next time
        rows = parse_rounds_file(raw)
        if rows is None:
            return  # part-way through being written, or empty
        self.last_stat = (st.st_mtime_ns, st.st_size)
        sha = hashlib.sha1(raw).hexdigest()
        info = summarize(rows)
        key = str(info["round_id"])
        self.state["current"] = key
        start_early_download(self.cfg, info["course"])
        rec = self.state["rounds"].get(key)
        if rec and rec["sha"] == sha:
            save_state(self.cfg, self.state)
            return
        saved = output_dir(self.cfg) / "saved" / f"{stem_for(info['course'], key)}.dat"
        write_atomic(saved, raw)
        os.utime(saved, ns=(st.st_mtime_ns, st.st_mtime_ns))  # the page's date comes from this time
        built = rec.get("built_sha") if rec else None
        self.state["rounds"][key] = dict(info, sha=sha, mtime=st.st_mtime, saved=str(saved), built_sha=built)
        save_state(self.cfg, self.state)
        log.info("round %s: %s, %d holes", key, info["course"], info["holes"])

    def tick(self, now=None):
        now = time.time() if now is None else now
        self.snapshot()
        for key, rec in list(self.state["rounds"].items()):
            if self.failed.get(key) == rec["sha"] or not is_due(rec, key == self.state.get("current"), now, self.cfg):
                continue
            if build_page(self.cfg, rec["saved"], rec["course"], rec["round_id"]):
                rec["built_sha"] = rec["sha"]
                save_state(self.cfg, self.state)
                self.next_publish = 0.0
            else:
                self.failed[key] = rec["sha"]  # try again if the round changes or the watcher restarts
        if self.cfg["publish_to"] and self.next_publish is not None and now >= self.next_publish:
            self.next_publish = None if publish(self.cfg, self.state) else now + PUBLISH_RETRY_SECONDS
        # a build sets next_publish back to 0.0, so None just means nothing is waiting


def setup_logging(cfg):
    output_dir(cfg).mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.handlers.RotatingFileHandler(output_dir(cfg) / "retrace.log", maxBytes=1_000_000, backupCount=1, encoding="utf-8")]
    if sys.stderr:  # there's no console under pythonw
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        log.addHandler(h)


def take_lock(cfg):
    """Hold a lock while running. Returns the open file, or None if another watcher has it."""
    output_dir(cfg).mkdir(parents=True, exist_ok=True)
    f = open(output_dir(cfg) / "watcher.lock", "a+b")
    try:
        import msvcrt
    except ImportError:
        return f
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        return None
    return f


def cmd_watch(cfg, a):
    setup_logging(cfg)
    lock = take_lock(cfg)
    if lock is None:
        log.info("a watcher is already running")
        return 0
    (output_dir(cfg) / "watcher.pid").write_text(str(os.getpid()), encoding="utf-8")
    if not courses_dir(cfg).is_dir():   # GSPro may have been installed or moved since setup
        found = find_courses(cfg)
        if found is not None:
            save_setting("courses_dir", found.as_posix())
            cfg["courses_dir"] = found.as_posix()
            log.info("found GSPro's courses in %s", found)
    log.info("watching %s", round_file(cfg))
    watcher = Watcher(cfg)
    while True:
        try:
            watcher.tick()
        except Exception:
            log.exception("watcher error; carrying on")
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------- other commands
def cmd_build(cfg, a):
    setup_logging(cfg)
    source = Path(a.file) if a.file else round_file(cfg)
    if not source.exists():
        log.error("round file not found: %s", source)
        return 1
    rows = parse_rounds_file(source.read_bytes())
    if rows is None:
        log.error("no round in %s (empty, or GSPro is still writing it)", source)
        return 1
    info = summarize(rows)
    if not build_page(cfg, source, info["course"], info["round_id"], a.date):
        return 1
    state = load_state(cfg)
    return 0 if publish(cfg, state) else 1


def merge_rows(first, second):
    """One round from two parts of it. A hole in both parts comes from the part that finished it (the first, if both
    did), and every player's holes are decided separately. Returns the rows and notes on what was decided."""
    def by_hole(rows):
        holes = {}
        for r in rows:
            holes.setdefault((r["UserGuid"], r["Hole"]), []).append(r)
        return holes

    a, b = by_hole(first), by_hole(second)
    finished = lambda rows: rows[-1]["HoleResult"] == 7
    notes, keep_a, keep_b = [], set(a), set(b)
    for key in sorted(set(a) & set(b), key=lambda k: k[1]):
        name, hole = a[key][0].get("PlayerName", "player"), key[1] + 1
        if finished(a[key]):
            keep_b.discard(key)
            notes.append(f"{name}, hole {hole}: in both parts; using the first" + (", as both finished it" if finished(b[key]) else ", as only it finished the hole"))
        else:
            keep_a.discard(key)
            notes.append(f"{name}, hole {hole}: in both parts; using the second, as the first didn't finish the hole")
    offset = max(r["GlobalShotNumber"] for r in first)    # each part numbers its shots from 1, and they set the play order
    round_id = first[-1]["RoundID"]
    merged = [r for r in first if (r["UserGuid"], r["Hole"]) in keep_a]
    merged += [dict(r, GlobalShotNumber=r["GlobalShotNumber"] + offset, RoundID=round_id) for r in second if (r["UserGuid"], r["Hole"]) in keep_b]
    return merged, notes


def cmd_merge(cfg, a):
    """Joins a round that GSPro split in two, such as when the PC restarted part-way through."""
    setup_logging(cfg)
    saved = output_dir(cfg) / "saved"
    parts = []
    for name in a.parts:
        matches = [Path(name)] if Path(name).is_file() else sorted(saved.glob(f"*-round-{name}.dat"))
        if not matches:
            print(f"no saved round {name} in {saved}")
            return 1
        rows = parse_rounds_file(matches[0].read_bytes())
        if rows is None:
            print(f"no round in {matches[0]}")
            return 1
        parts.append((matches[0], rows))
    parts.sort(key=lambda p: p[1][-1]["RoundID"])
    (path1, rows1), (path2, rows2) = parts
    info1, info2 = summarize(rows1), summarize(rows2)
    if info1["round_id"] == info2["round_id"] or info1["course"] != info2["course"]:
        print(f"these can't be joined: round {info1['round_id']} on {info1['course']}, and round {info2['round_id']} on {info2['course']}")
        return 1

    # the watcher keeps the state in memory and would write over this, so it has to be stopped first
    was_running = watcher_running(cfg)
    if was_running:
        cmd_stop(cfg, a)
    merged, notes = merge_rows(rows1, rows2)
    for note in notes:
        print(note)
    info = summarize(merged)
    source = saved / f"{stem_for(info['course'], info['round_id'])}-merged.dat"
    write_atomic(source, json.dumps(merged, separators=(",", ":")).encode("utf-8"))
    latest = max(path1.stat().st_mtime, path2.stat().st_mtime)
    os.utime(source, (latest, latest))                    # the page's date comes from this time
    print(f"Joining rounds {info1['round_id']} and {info2['round_id']}: {info['holes']} holes. Building the page...")
    page = build_page(cfg, source, info["course"], info["round_id"])
    if page:
        (rounds_dir(cfg) / f"{stem_for(info2['course'], info2['round_id'])}.html").unlink(missing_ok=True)
        make_index.write_index(rounds_dir(cfg))
        state = load_state(cfg)
        for part in (info1, info2):                       # the parts stay in saved, but shouldn't get pages again
            rec = state["rounds"].get(str(part["round_id"]))
            if rec:
                rec["built_sha"] = rec["sha"]
        save_state(cfg, state)
        print("Built", page)
        if cfg["publish_to"]:
            publish(cfg, state)
            print(f"Round {info2['round_id']}'s old page is still in your bucket; delete it there if you don't want it.")
    else:
        print("The build failed; the reason is in", output_dir(cfg) / "retrace.log")
    if was_running:
        subprocess.Popen([str(Path(sys.executable).with_name("pythonw.exe")), str(HERE / "retrace.py"), "watch"], cwd=HERE,
                         creationflags=subprocess.DETACHED_PROCESS)
    return 0 if page else 1


def cmd_index(cfg, a):
    count = make_index.write_index(rounds_dir(cfg))
    print(f"{rounds_dir(cfg) / make_index.INDEX}: {count} round(s)")
    return 0


def cmd_open(cfg, a):
    make_index.write_index(rounds_dir(cfg))
    os.startfile(rounds_dir(cfg) / make_index.INDEX)
    return 0


def cmd_publish(cfg, a):
    setup_logging(cfg)
    if not cfg["publish_to"]:
        log.error("no bucket set; to set one, run:\n       %s", run_me("set publish_to gs://your-bucket"))
        return 1
    make_index.write_index(rounds_dir(cfg))
    return 0 if publish(cfg, load_state(cfg)) else 1


def cmd_set(cfg, a):
    raw = load_config_file()
    if a.key.startswith("location."):
        raw.setdefault("locations", {})[a.key[len("location."):]] = a.value
    elif a.key not in DEFAULTS or a.key == "locations":
        print("unknown setting:", a.key, "\nsettings:", ", ".join(k for k in DEFAULTS if k != "locations"), "or location.<CourseKey>")
        return 1
    elif isinstance(DEFAULTS[a.key], bool):
        if a.value.lower() not in ("on", "off", "true", "false", "yes", "no", "1", "0"):
            print(f"{a.key} is on or off")
            return 1
        raw[a.key] = a.value.lower() in ("on", "true", "yes", "1")
    elif isinstance(DEFAULTS[a.key], int):
        raw[a.key] = int(a.value)
    elif a.value:
        raw[a.key] = a.value
    else:
        raw.pop(a.key, None)
    write_atomic(CONFIG, json.dumps(raw, indent=2).encode("utf-8"))
    print(f"set {a.key} = {a.value!r} in {CONFIG}")
    if watcher_running(cfg):
        print(RESTART_NOTE)
    return 0


def load_config_file():
    return json.loads(CONFIG.read_text(encoding="utf-8-sig")) if CONFIG.exists() else {}


def startup_file():
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Retrace.cmd"


def cmd_startup(cfg, a):
    path = startup_file()
    if a.state == "off":
        path.unlink(missing_ok=True)
        print("removed", path)
        return 0
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        print("pythonw.exe not found next to", sys.executable)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'@echo off\nstart "" "{pythonw}" "{HERE / "retrace.py"}" watch\n', encoding="mbcs" if os.name == "nt" else "utf-8")
    print("the watcher will start when you sign in:", path)
    return 0


def watcher_running(cfg):
    lock = take_lock(cfg)
    if lock is None:
        return True
    lock.close()
    return False


def cmd_stop(cfg, a):
    if not watcher_running(cfg):
        print("the watcher isn't running")
        return 0
    pid = (output_dir(cfg) / "watcher.pid").read_text(encoding="utf-8").strip()
    done = subprocess.run(["taskkill", "/pid", pid, "/f"], capture_output=True, text=True)
    print(done.stdout.strip() or done.stderr.strip())
    return done.returncode


def cmd_check(cfg, a):
    problems = 0

    def line(ok, label, detail=""):
        nonlocal problems
        problems += ok is False
        mark = {True: "ok ", False: "!! ", None: "-  "}[ok]
        print(f"{mark} {label}{': ' + str(detail) if detail else ''}")

    for mod, pip_name in (("numpy", "numpy"), ("scipy", "scipy"), ("PIL", "pillow"), ("rasterio", "rasterio"), ("shapely", "shapely"), ("pyproj", "pyproj"), ("laspy", "laspy")):
        try:
            __import__(mod)
            line(True, f"Python package {pip_name}")
        except ImportError:
            line(False, f"Python package {pip_name}", "missing; run Install.bat again")
    rf = round_file(cfg)
    if rf.exists():
        line(True, "Round file", rf)
    else:
        line(None, "Round file not there yet", f"{rf} (GSPro creates it during a round; if you've played one, set round_file)")
    cd = courses_dir(cfg)
    line(cd.is_dir(), "Courses folder", cd if cd.is_dir() else f"{cd} not found. To point Retrace at your Courses folder (GSPro shows it under Settings > Game > Course Folder), run these in a command window:\n       " + run_me('find-courses "D:/your/Courses"'))
    if rf.exists():
        rows = parse_rounds_file(rf.read_bytes())
        if rows is None:
            line(None, "Latest round", "the file has no finished write right now")
        else:
            info = summarize(rows)
            line(None, "Latest round", f"{info['course']}, round {info['round_id']}, {info['holes']} holes" + (", all holed out" if info["all_holed"] else ""))
            has_gkd = any((cd / info["course"]).glob("*.[gG][kK][dD]")) if (cd / info["course"]).is_dir() else False
            line(has_gkd or None, "Course data for that round", "found" if has_gkd else "no .GKD file, so the page will use the shot data only")
            where = course_location(cfg, info["course"])
            line(True if where else None, "Course location", f"{where[0]:.5f}, {where[1]:.5f}" if where else "not known, so its page won't have real terrain")
            state = course_sources(cfg, info["course"])
            terrain = {"lidar": "lidar", "tiles": "worldwide terrain tiles"}.get(state.get("elevation")) if state.get("aligned") else None
            line(None, "Real terrain", terrain or "downloads automatically when a round on this course finishes")
    line(True, "Pages go to", rounds_dir(cfg))
    target = cfg["publish_to"]
    if target:
        try:
            upload_command(target, Path("x"))
            line(True, "Publishing to", target)
        except RuntimeError as e:
            line(False, "Publishing to " + target, e)
    else:
        line(None, "Publishing", "off")
    line(True if watcher_running(cfg) else None, "Watcher", "running" if watcher_running(cfg) else "not running")
    if os.environ.get("APPDATA"):
        line(True if startup_file().exists() else None, "Start at sign-in", "on" if startup_file().exists() else "off")
    print("\nAll good." if not problems else f"\n{problems} problem(s) to fix.")
    return 1 if problems else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    sub.add_parser("watch").set_defaults(fn=cmd_watch)
    p = sub.add_parser("build")
    p.add_argument("file", nargs="?", help="a saved round file (default: GSPro's current round)")
    p.add_argument("--date", help="date the round was played, YYYY-MM-DD (default: when the file was last written)")
    p.set_defaults(fn=cmd_build)
    sub.add_parser("open").set_defaults(fn=cmd_open)
    sub.add_parser("index").set_defaults(fn=cmd_index)
    sub.add_parser("publish").set_defaults(fn=cmd_publish)
    p = sub.add_parser("set")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(fn=cmd_set)
    p = sub.add_parser("merge")
    p.add_argument("parts", nargs=2, metavar="ROUND", help="a saved round's number, or its file")
    p.set_defaults(fn=cmd_merge)
    p = sub.add_parser("find-courses")
    p.add_argument("folder", nargs="?", help="look only in this folder (default: search this PC, if the folder isn't known yet)")
    p.set_defaults(fn=cmd_find_courses)
    p = sub.add_parser("startup")
    p.add_argument("state", choices=("on", "off"))
    p.set_defaults(fn=cmd_startup)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    a = ap.parse_args()
    sys.exit(a.fn(load_config(), a))


if __name__ == "__main__":
    main()
