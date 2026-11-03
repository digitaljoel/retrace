"""Write index.html: a list of every round page in a folder, newest first.

    python make_index.py [folder]            (default: this folder)

Everything it shows is read back out of the round pages themselves, and it links to them by relative
path, so the folder works the same opened from disk or served from a bucket.
"""
import datetime as dt, html, json, os, sys
from pathlib import Path
from urllib.parse import quote

INDEX = "index.html"
MARKER = '<script type="application/json" id="round-data">'


def read_round(path):
    """A summary of one round page, or None if the file isn't one."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    start = text.find(MARKER)
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text, start + len(MARKER))
    except ValueError:
        return None  # the template itself, or a damaged page
    course = data["course"]
    players = []
    for p in data["players"]:
        played = [(h, c) for h, c in zip(p["holes"], course["holes"]) if h and h["complete"]]
        strokes = sum(h["strokes"] for h, _ in played)
        par = sum(c["par"] for _, c in played)
        players.append({"name": p["name"], "strokes": strokes, "to_par": strokes - par, "thru": len(played)})
    players.sort(key=lambda p: (-p["thru"], p["to_par"]))
    return {"file": Path(path).name, "course": course["name"], "location": course.get("location") or "",
            "holes": len(course["holes"]), "played": data["played"], "day": data["playedDay"], "players": players}


def read_rounds(folder):
    rounds = []
    for path in sorted(Path(folder).glob("*.html")):
        if path.name == INDEX:
            continue
        try:
            r = read_round(path)
        except (OSError, KeyError, TypeError) as e:
            print(f"skipping {path.name}: {e}", file=sys.stderr)
            continue
        if r:
            rounds.append(r)
    rounds.sort(key=lambda r: r["played"], reverse=True)
    return rounds


def to_par_text(n):
    return "E" if n == 0 else f"{n:+d}".replace("-", "−")


def nice_day(day):
    d = dt.datetime.strptime(day, "%Y-%m-%d")
    return f"{d:%b} {d.day}, {d.year}"


def row_html(r):
    e = html.escape
    scores = []
    for p in r["players"]:
        if p["thru"] == 0:
            score = '<span class="thru">no holes finished</span>'
        else:
            cls = "par under" if p["to_par"] < 0 else "par"
            thru = f'<span class="thru">thru {p["thru"]}</span>' if p["thru"] < r["holes"] else ""
            score = f'<b>{p["strokes"]}</b><span class="{cls}">{to_par_text(p["to_par"])}</span>{thru}'
        scores.append(f'<li><span class="who">{e(p["name"])}</span><span class="score">{score}</span></li>')
    place = f'{r["holes"]} holes' + (f' · {e(r["location"])}' if r["location"] else "")
    return (f'<li class="round" data-course="{e(r["course"], quote=True)}"><a href="{quote(r["file"])}">'
            f'<time datetime="{e(r["day"])}">{e(nice_day(r["day"]))}</time>'
            f'<span class="course"><strong>{e(r["course"])}</strong><small>{place}</small></span>'
            f'<ul class="players">{"".join(scores)}</ul></a></li>')


def page_html(rounds):
    courses = sorted({r["course"] for r in rounds})
    count = f'{len(rounds)} round{"" if len(rounds) == 1 else "s"}'
    picker = ""
    if len(courses) > 1:
        options = "".join(f'<option value="{html.escape(c, quote=True)}">{html.escape(c)}</option>' for c in courses)
        picker = f'<label class="pick">Course <select id="course"><option value="">All courses</option>{options}</select></label>'
    if rounds:
        body = f'<ol class="rounds">{"".join(row_html(r) for r in rounds)}</ol>'
    else:
        body = '<p class="empty">No rounds yet. Finish a round in GSPro and it will show up here.</p>'
    return PAGE.replace("__COUNT__", count).replace("__PICKER__", picker).replace("__BODY__", body)


def write_index(folder):
    """Write the folder's index.html, leaving it untouched if nothing changed. Returns the round count."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    rounds = read_rounds(folder)
    page = page_html(rounds).encode("utf-8")
    out = folder / INDEX
    if not out.exists() or out.read_bytes() != page:
        tmp = folder / (INDEX + ".tmp")
        tmp.write_bytes(page)
        os.replace(tmp, out)
    return len(rounds)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Retrace</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=Big+Shoulders+Display:wght@700;800;900&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<script>
try { const t = localStorage.getItem('gspro-viewer-theme'); if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t); } catch (e) { /* follow the system */ }
</script>
<style>
:root {
  --sky-top: #8DBCD8; --sky-bottom: #E4ECE3; --panel: rgba(249, 251, 247, 0.93); --hover: #FFFFFF;
  --ink: #14211A; --ink-2: #54655C; --line: rgba(20, 33, 26, 0.13); --under: #C32F25; --focus: #1D63D6;
  --shadow: 0 10px 30px rgba(18, 38, 30, 0.16), 0 1px 2px rgba(18, 38, 30, 0.12);
  --f-display: "Big Shoulders Display", "Arial Narrow", Impact, sans-serif;
  --f-body: Archivo, "Helvetica Neue", Arial, sans-serif;
  --f-data: "IBM Plex Mono", ui-monospace, Menlo, Consolas, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --sky-top: #07111A; --sky-bottom: #263A41; --panel: rgba(15, 23, 26, 0.91); --hover: #1A272B;
    --ink: #E3ECE6; --ink-2: #97AAA1; --line: rgba(227, 236, 230, 0.14); --under: #FF7B6B; --focus: #84B4FF;
    --shadow: 0 10px 30px rgba(0, 0, 0, 0.45), 0 1px 2px rgba(0, 0, 0, 0.4);
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --sky-top: #07111A; --sky-bottom: #263A41; --panel: rgba(15, 23, 26, 0.91); --hover: #1A272B;
  --ink: #E3ECE6; --ink-2: #97AAA1; --line: rgba(227, 236, 230, 0.14); --under: #FF7B6B; --focus: #84B4FF;
  --shadow: 0 10px 30px rgba(0, 0, 0, 0.45), 0 1px 2px rgba(0, 0, 0, 0.4);
  color-scheme: dark;
}
[hidden] { display: none !important; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; color: var(--ink); font-family: var(--f-body); -webkit-font-smoothing: antialiased;
  background: linear-gradient(180deg, var(--sky-top) 0%, var(--sky-bottom) 72%) fixed;
}
.wrap { max-width: 860px; margin: 0 auto; padding: 40px 16px 56px; }
header { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 12px 24px; margin-bottom: 20px; }
h1 { font: 900 56px/0.95 var(--f-display); letter-spacing: 0.02em; text-transform: uppercase; margin: 0; }
.count { font: 500 13px/1.3 var(--f-data); color: var(--ink-2); margin: 6px 0 0; }
.pick { font: 500 13px/1 var(--f-body); color: var(--ink-2); display: flex; align-items: center; gap: 8px; }
select { font: 500 14px/1.2 var(--f-body); color: var(--ink); background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; max-width: 62vw; }
ol, ul { list-style: none; margin: 0; padding: 0; }
.rounds { display: grid; gap: 10px; }
.round a {
  display: grid; grid-template-columns: 96px minmax(0, 1fr) minmax(190px, auto); gap: 8px 18px; align-items: center;
  padding: 15px 18px; color: inherit; text-decoration: none; background: var(--panel); border-radius: 12px; box-shadow: var(--shadow);
  transition: background 0.12s ease, transform 0.12s ease;
}
.round a:hover { background: var(--hover); transform: translateY(-1px); }
a:focus-visible, select:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
time { font: 500 12.5px/1.3 var(--f-data); color: var(--ink-2); }
.course { min-width: 0; }
.course strong { display: block; font: 800 25px/1.05 var(--f-display); letter-spacing: 0.02em; text-transform: uppercase; overflow-wrap: anywhere; }
.course small { display: block; font-size: 12.5px; line-height: 1.35; color: var(--ink-2); margin-top: 3px; }
.players { display: grid; gap: 5px; }
.players li { display: flex; align-items: baseline; justify-content: space-between; gap: 14px; }
.who { font-size: 14px; font-weight: 500; min-width: 0; overflow-wrap: anywhere; }
.score { font: 500 13px/1.3 var(--f-data); white-space: nowrap; }
.score b { font-weight: 600; font-size: 15px; }
.par { color: var(--ink-2); margin-left: 7px; }
.par.under { color: var(--under); }
.thru { color: var(--ink-2); font-size: 11.5px; margin-left: 7px; }
.empty { background: var(--panel); border-radius: 12px; box-shadow: var(--shadow); padding: 26px 20px; margin: 0; color: var(--ink-2); }
footer { margin-top: 26px; font-size: 12px; line-height: 1.5; color: var(--ink-2); }
@media (max-width: 640px) {
  .wrap { padding-top: 26px; }
  h1 { font-size: 44px; }
  .round a { grid-template-columns: minmax(0, 1fr); padding: 14px 16px; }
  .players { border-top: 1px solid var(--line); padding-top: 8px; margin-top: 2px; }
}
@media (prefers-reduced-motion: reduce) { .round a { transition: none; } .round a:hover { transform: none; } }
</style>
</head>
<body>
<div class="wrap">
<header>
  <div><h1>Retrace</h1><p class="count" id="count">__COUNT__</p></div>
  __PICKER__
</header>
<main>__BODY__</main>
<footer>Replays of rounds played in the GSPro golf simulator.</footer>
</div>
<script>
const pick = document.getElementById('course');
if (pick) pick.addEventListener('change', () => {
  let shown = 0;
  document.querySelectorAll('.round').forEach(r => {
    r.hidden = pick.value !== '' && r.dataset.course !== pick.value;
    if (!r.hidden) shown++;
  });
  document.getElementById('count').textContent = shown + (shown === 1 ? ' round' : ' rounds');
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    print(f"{os.path.join(target, INDEX)}: {write_index(target)} round(s)")
