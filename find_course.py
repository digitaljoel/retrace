"""Find a real course on OpenStreetMap from the name and town in its GSPro course file.

    python find_course.py "Colonial Country Club" "Fort Worth, TX, USA"

Many course files have no latitude and longitude, only a name and a town. This finds the town with
OpenStreetMap's place search (Nominatim), then searches near it for golf courses, picking the one whose
name shares the most distinctive words with the course file's name. A wrong pick can't put a map in the wrong place: the course
still has to line up with the map and terrain before either is used.
"""
import difflib, http.client, json, math, re, sys, time, urllib.error, urllib.parse, urllib.request

HEADERS = {"User-Agent": "retrace/0.1 (golf replay viewer)", "Accept": "application/json"}
NEAR_KM = 40
# words that say nothing about which course it is
COMMON = {"golf", "club", "course", "courses", "country", "the", "of", "at", "and", "gc", "cc", "g", "c", "links",
          "resort", "gsp", "hd", "v1", "v2", "v3", "national", "municipal", "muni", "park"}


def get(url, tries=4):
    problem = None
    for attempt in range(tries):
        u = url
        try:
            req = urllib.request.Request(u, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise
            problem = f"HTTP {e.code}"
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, ValueError) as e:
            problem = str(getattr(e, "reason", e))[:200]
        if attempt < tries - 1:
            time.sleep(10 * 2 ** attempt)
    raise RuntimeError(f"no answer from {urllib.parse.urlsplit(u).hostname}: {problem}")


def words(name):
    return {w for w in re.findall(r"[a-z0-9]+", (name or "").lower().replace("'", "")) if w not in COMMON}


def town_places(town):
    """Where the town might be, trying the full text, then without the parts that don't geocode."""
    parts = [p.strip() for p in town.split(",") if p.strip()]
    tried = []
    for q in [", ".join(parts), ", ".join(p for p in parts if len(p) > 1), parts[0] if parts else ""]:
        if q and q not in tried:
            tried.append(q)
            hits = nominatim(q=q, limit=3)
            if hits:
                return [(float(h["lat"]), float(h["lon"])) for h in hits]
    return []


def nominatim(**params):
    time.sleep(1)   # Nominatim asks for no more than one request a second
    return get("https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(dict(format="json", **params)))


def find(name, town, say=print):
    """(lat, lon, OSM name) of the best-matching golf course near the town, or None."""
    want = words(name)
    if not want or not town:
        say(f"  can't look it up: the course file names {'no town' if want else 'the course only in common words'}")
        return None
    places = town_places(town)
    if not places:
        say(f"  OpenStreetMap doesn't know the town \"{town}\"")
        return None
    lat, lon = places[0]
    d = NEAR_KM / 111.3
    box = f"{lon - d / math.cos(math.radians(lat)):.4f},{lat + d:.4f},{lon + d / math.cos(math.radians(lat)):.4f},{lat - d:.4f}"
    best, seen = None, set()
    # the place search matches every word it's given, so search each distinctive word on its own
    for w in sorted(want, key=len, reverse=True)[:4]:
        for h in nominatim(q=f"{w} golf", viewbox=box, bounded=1, limit=20) + nominatim(q=w, viewbox=box, bounded=1, limit=20):
            if h.get("type") != "golf_course" or h["osm_id"] in seen:
                continue
            seen.add(h["osm_id"])
            theirs = words(h.get("name") or h["display_name"].split(",")[0])
            # a word counts if it's in the course file's name, give or take a letter ("Koasati", "Kosati")
            shared = sum(any(difflib.SequenceMatcher(None, a, b).ratio() >= 0.8 for b in want) for a in theirs)
            # all of the mapped name's own words, or at least two, so a lone shared "Pines" or "Fish" isn't enough
            if not shared or (shared < len(theirs) and shared < 2):
                continue
            km = math.hypot(float(h["lat"]) - lat, (float(h["lon"]) - lon) * math.cos(math.radians(lat))) * 111.3
            score = (shared / len(theirs), shared, -km)
            if not best or score > best[0]:
                best = (score, float(h["lat"]), float(h["lon"]), h.get("name") or h["display_name"].split(",")[0])
    if not best:
        say(f"  no golf course named like \"{name}\" is mapped within {NEAR_KM} km of {town}")
        return None
    return best[1], best[2], best[3]


if __name__ == "__main__":
    print(find(*sys.argv[1:3]))
