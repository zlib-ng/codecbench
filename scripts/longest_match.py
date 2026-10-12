#!/usr/bin/env python3
"""Chart zlib-ng's longest_match benchmark for two builds, by data type and level.

The benchmark replays the match search at every position of a window with
every position hashed, under the chain and match limits of five deflate
levels, and reports the time per sweep and the average match length. Each
data type gets a panel, time per position across on one log scale and
average match length up, the levels joined into a ladder that shows what a
deeper search costs and what it finds. Two builds of one search land on the
same spots at that scale, so beneath each panel a bar per level gives the
change in time per position from the reference run, over the band
repetition noise covers, with the two times written beside it.

Either side may be several passes of one build joined by commas. A row then
takes its fastest pass, contention only ever adds time, and the gap to its
next fastest pass widens its noise band. Two processes of one binary differ
by more than the repetitions inside either on rows this short, so a single
pass per side understates the noise.

Usage:
    scripts/longest_match.py base.json[,base2.json] head.json[,head2.json]
        [-o out.svg] [--names a,b] [--title text] [--filter regex]
        [--impl neon]
    scripts/longest_match.py --measure <ref> [-o out.json] [--reps 5]
        [--workdir .pr-bench]

--measure builds zlib-ng's benchmark_zlib at one revision, pr/<number> for
a pull request head, in a worktree under --workdir (lm-<rev>,
build-lm-<rev>), runs the longest_match rows through run_clean.py and
records the commit in the JSON. One revision per call keeps each benchmark
process under ten minutes. The numbers are also printed to stdout.
"""
import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path

from compare_inputs import fmt_pct
from compare_levels import NOISE_CVS, NOISE_FLOOR, change, commit, text_width
from frontier import noise
from graph_runs import (GRID, INK, INK_SOFT, SERIES, SURFACE, Svg, esc, footnote, load,
                        machine_line, order_types, run_warnings)
from symbol_cost import LESS, MORE, nice

UPSTREAM = "https://github.com/zlib-ng/zlib-ng.git"
SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

NAME_RE = re.compile(r"^longest_match_(?:slow_(?P<slow>knuth|roll)_)?(?P<impl>[a-z0-9]+)"
                     r"/(?P<data>\w+)/(?P<level>\d+)$")
# What each benchmark family replays, in plain words
SEARCHES = {None: "fast and medium search", "knuth": "slow search",
            "roll": "slow search, rolling hash"}
# A change inside the noise band takes the neutral slot
NEUTRAL = SERIES[-1]

WIDTH, COLS = 1080, 3
PANEL_H = 158
PAD_L, PAD_R, PAD_T, PAD_B = 30, 8, 22, 20
GAP_X, GAP_Y = 24, 30
# The change bars beneath a panel, a row per level
STRIP_W, STRIP_ROW, BAR_H = 160, 14, 8


def sh(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run(cmd, check=True, **kw)


def out(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True,
                          **kw).stdout.strip()


def measure(args):
    """Build benchmark_zlib at one revision and run its longest_match rows."""
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    repo = workdir / "zlib-ng"
    if not repo.exists():
        sh(["git", "clone", UPSTREAM, str(repo)])
    pr = re.fullmatch(r"pr/(\d+)", args.measure)
    ref = f"refs/pr/{pr.group(1)}" if pr else args.measure
    sh(["git", "-C", str(repo), "fetch", "origin", "develop"]
       + ([f"+pull/{pr.group(1)}/head:{ref}"] if pr else []))
    rev = out(["git", "-C", str(repo), "rev-parse", f"{ref}^{{commit}}"])
    tree = workdir / f"lm-{rev[:8]}"
    if not tree.exists():
        sh(["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), rev])
    build_dir = workdir / f"build-lm-{rev[:8]}"
    sh(["cmake", "-B", str(build_dir), "-S", str(tree), "-DCMAKE_BUILD_TYPE=Release",
        "-DWITH_BENCHMARKS=ON", "-DWITH_GTEST=OFF"], stdout=subprocess.DEVNULL)
    sh(["cmake", "--build", str(build_dir), "-j", "--target", "benchmark_zlib"],
       stdout=subprocess.DEVNULL)
    out_json = Path(args.output) if args.output else workdir / f"lm-{rev[:8]}.json"
    sh([sys.executable, str(SCRIPTS / "run_clean.py"),
        str(build_dir / "test" / "benchmarks" / "benchmark_zlib"),
        "--filter", "^longest_match_", "--data-types", "all", "--reps", str(args.reps),
        "--out", str(out_json)])
    # benchmark_zlib does not know its commit, the charts read it from the context
    doc = json.loads(out_json.read_text())
    doc["context"]["codec_version"] = out(["git", "-C", str(tree), "describe", "--tags", "--always"])
    out_json.write_text(json.dumps(doc))
    print(out_json)


def load_run(path):
    """graph_runs.load with the repetition cv read from cpu time, these rows carry no byte rate."""
    name, version, benchmarks, context = load(path)
    with open(path) as f:
        for b in json.load(f)["benchmarks"]:
            if b.get("aggregate_name") == "cv" and b["run_name"] in benchmarks:
                benchmarks[b["run_name"]]["_cv"] = b.get("cpu_time", 0.0)
    return name, version, benchmarks, context


def match_rows(benchmarks):
    """{impl: {data type: {level: time, len, cv, slow}}} of one run, time in ns per position."""
    rows = {}
    for name, b in benchmarks.items():
        m = NAME_RE.match(name)
        rate = b.get("Items/sec", 0.0)
        # A family the cpu cannot run reports an error instead of a rate
        if not m or b.get("error_occurred") or rate <= 0:
            continue
        rows.setdefault(m["impl"], {}).setdefault(m["data"], {})[int(m["level"])] = {
            "time": 1e9 / rate, "len": b.get("avg_len", 0.0), "cv": b.get("_cv", 0.0),
            "slow": m["slow"]}
    return rows


def fastest(passes):
    """One build's rows over its passes, each row from its fastest pass with the gap to the next
    fastest kept as its spread."""
    seen = {}
    for rows in passes:
        for impl, types in rows.items():
            for t, ladder in types.items():
                for level, row in ladder.items():
                    seen.setdefault(impl, {}).setdefault(t, {}).setdefault(level, []).append(row)
    for types in seen.values():
        for ladder in types.values():
            for level, found in ladder.items():
                found.sort(key=lambda row: row["time"])
                gap = found[1]["time"] / found[0]["time"] - 1 if len(found) > 1 else 0.0
                ladder[level] = {**found[0], "spread": gap}
    return seen


def fmt_ns(ns, like=None):
    """A time per position, in the unit and digits that suit like when two are shown together."""
    ref = like if like is not None else ns
    if ref >= 1000:
        return f"{ns / 1000:.{1 if ref >= 10000 else 2}f} µs"
    return f"{ns:.{0 if ref >= 100 else 1 if ref >= 10 else 2}f} ns"


def fmt_pair(a, b):
    """Two times in one unit, written once."""
    ref = max(a, b)
    return f"{fmt_ns(a, ref).split()[0]} → {fmt_ns(b, ref)}"


def len_ticks(top):
    """Round ticks from zero to past top, with headroom for the labels above the highest point."""
    step = 10 ** math.floor(math.log10(top))
    step = next(m * step for m in (0.2, 0.5, 1, 2, 5, 10) if top / (m * step) <= 4)
    return [i * step for i in range(math.ceil(top * 1.1 / step) + 1)]


def label_sides(prev, here, nxt):
    """The diagonals off a point, the one clearest of the ladder's lines to its neighbors first."""
    taken = [math.atan2(q[1] - here[1], q[0] - here[0]) for q in (prev, nxt) if q]

    def clearance(a):
        return min((abs(math.remainder(a - t, math.tau)) for t in taken), default=math.pi)
    # up and left leads a tie, a rising ladder leaves that side free
    return sorted((-0.75 * math.pi, 0.25 * math.pi, -0.25 * math.pi, 0.75 * math.pi),
                  key=clearance, reverse=True)


def mark(svg, i, x, y, title=None):
    """The reference run is an open ring and the other a dot, so a pair that coincides shows both."""
    if i == 0:
        body = (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6.5" fill="none" stroke="{SERIES[0]}" '
                f'stroke-width="2"/>')
    else:
        body = (f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{SERIES[1]}" stroke="{SURFACE}" '
                f'stroke-width="1.5"/>')
    svg.add(f"<g>{body}<title>{esc(title)}</title></g>" if title else body)


def ladder_line(svg, i, pts, dashed):
    """One segment of a ladder, dashed where it runs into the slow search."""
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    path = " ".join(f"{'M' if k == 0 else 'L'}{x:.1f},{y:.1f}" for k, (x, y) in enumerate(pts))
    svg.add(f'<path d="{path}" fill="none" stroke="{SERIES[i]}" stroke-width="2" '
            f'stroke-linecap="round" stroke-linejoin="round"{dash}/>')


def panel(svg, x0, y0, w, label, levels, ladders, names, xlo, xhi):
    """One data type, ladders maps a run index to {level: row}."""
    px, py = x0 + PAD_L, y0 + PAD_T
    pw, ph = w - PAD_L - PAD_R, PANEL_H - PAD_T - PAD_B
    yticks = len_ticks(max(p["len"] for ladder in ladders.values() for p in ladder.values()))

    def sx(t):
        return px + math.log(t / xlo) / math.log(xhi / xlo) * pw

    def sy(n):
        return py + ph - n / yticks[-1] * ph

    svg.text(px, y0 + 13, label, 12, INK, weight="bold")
    svg.add(f'<rect x="{px:.1f}" y="{py:.1f}" width="{pw:.1f}" height="{ph:.1f}" fill="none" '
            f'stroke="{GRID}" stroke-width="1"/>')
    for e in range(math.ceil(math.log10(xlo)), math.floor(math.log10(xhi)) + 1):
        svg.line(sx(10 ** e), py, sx(10 ** e), py + ph, GRID, 0.5)
        svg.text(sx(10 ** e), py + ph + 13, f"{10 ** e:g} ns" if e < 3 else f"{10 ** (e - 3):g} µs",
                 9, INK_SOFT, anchor="middle")
    for n in yticks:
        svg.line(px, sy(n), px + pw, sy(n), GRID, 0.5)
        svg.text(px - 4, sy(n) + 3, f"{n:g}", 9, INK_SOFT, anchor="end")

    at = {i: {l: (sx(p["time"]), sy(p["len"])) for l, p in ladder.items()}
          for i, ladder in ladders.items()}
    for i in (0, 1):
        kept = [l for l in levels if l in at[i]]
        for a, b in zip(kept, kept[1:]):
            ladder_line(svg, i, [at[i][a], at[i][b]], bool(ladders[i][b]["slow"]))
    for i in (0, 1):
        for level, (x, y) in at[i].items():
            p = ladders[i][level]
            mark(svg, i, x, y, f"{label} level {level}, {names[i]} - {fmt_ns(p['time'])} per "
                 f"position, {p['len']:.3f} bytes")

    # Levels that land on one spot share a label
    spots = []
    for level in (l for l in levels if l in at[1]):
        if spots and math.dist(at[1][level], spots[-1][1][0]) < 12:
            spots[-1][0].append(level)
            spots[-1][1].append(at[1][level])
        else:
            spots.append(([level], [at[1][level]]))
    centers = [(sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts))
               for _, pts in spots]
    placed = []
    for k, (group, pts) in enumerate(spots):
        text = "·".join(str(l) for l in group)
        tw = text_width(text, 9.5)
        others = [q for i in (0, 1) for l, q in at[i].items() if l not in group]
        best = None
        for a in label_sides(centers[k - 1] if k else None, centers[k],
                             centers[k + 1] if k + 1 < len(spots) else None):
            left, up = math.cos(a) < 0, math.sin(a) < 0
            x = min(x for x, _ in pts) - 7 if left else max(x for x, _ in pts) + 7
            y = min(y for _, y in pts) - 7 if up else max(y for _, y in pts) + 15
            box = (x - tw if left else x, y - 8, x if left else x + tw, y + 1)
            best = best or (x, y, left, box)
            # Inside the frame and clear of its tick labels, off the other levels' marks and off
            # the labels already set
            if (box[0] >= px + 6 and box[2] <= px + pw and box[1] >= py and box[3] <= py + ph
                    and not any(box[0] - 7 < qx < box[2] + 7 and box[1] - 7 < qy < box[3] + 7
                                for qx, qy in others)
                    and not any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
                                for b in placed)):
                best = (x, y, left, box)
                break
        x, y, left, box = best
        placed.append(box)
        svg.text(x, y, text, 9.5, INK, anchor="end" if left else "start")


def strip(svg, px, y, levels, cells, scale):
    """A bar per level for the change in time per position, on one symmetric scale.

    cells maps a level to (percent change, noise band, real, the two times, tooltip).
    """
    mid, half = px + STRIP_W / 2, STRIP_W / 2 - 2
    svg.text(px - 6, y - 4, "level", 9, INK_SOFT, anchor="end")
    svg.add('<g style="font-variant-numeric: tabular-nums">')
    for j, level in enumerate(levels):
        ry = y + j * STRIP_ROW
        svg.text(px - 6, ry + 10.5, str(level), 9.5, INK, anchor="end")
        if level not in cells:
            continue
        d, band, real, times, tip = cells[level]
        bw = min(band, scale) / scale * half
        body = (f'<rect x="{mid - bw:.1f}" y="{ry + 1}" width="{2 * bw:.1f}" '
                f'height="{STRIP_ROW - 2}" fill="{GRID}"/>')
        w = abs(d) / scale * half
        if w > 0.5:
            hue = (LESS if d < 0 else MORE) if real else NEUTRAL
            body += (f'<rect x="{mid - w if d < 0 else mid:.1f}" y="{ry + (STRIP_ROW - BAR_H) / 2}" '
                     f'width="{w:.1f}" height="{BAR_H}" rx="2" fill="{hue}"/>')
        svg.add(f"<g>{body}<title>{esc(tip)}</title></g>")
        svg.text(px + STRIP_W + 44, ry + 10.5, fmt_pct(round(d, 1)), 9.5, INK if real else INK_SOFT,
                 anchor="end", weight="bold" if real else "normal")
        if times:
            svg.text(px + STRIP_W + 54, ry + 10.5, times, 9.5, INK_SOFT)
    svg.add("</g>")
    bottom = y + len(levels) * STRIP_ROW
    svg.line(mid, y, mid, bottom, INK_SOFT, 0.75)
    for side, text in ((-1, f"-{scale:g}%"), (0, "0"), (1, f"+{scale:g}%")):
        svg.text(mid + side * half, bottom + 11, text, 9, INK_SOFT, anchor="middle")
    return bottom + 14


def wrap(text, size, width):
    """Break text into lines that fit a width, by the rough glyph widths."""
    lines = [""]
    for word in text.split():
        if lines[-1] and text_width(f"{lines[-1]} {word}", size) > width:
            lines.append(word)
        else:
            lines[-1] = f"{lines[-1]} {word}".strip()
    return lines


def render(names, versions, machine, warnings, impl, types, levels, rows, passes, title,
           out_path):
    svg = Svg(WIDTH)
    svg.text(16, 30, title, 20, INK, weight="bold")
    built = [f"{names[i]} {commit(v)}".strip() for i, v in enumerate(versions)]
    svg.text(16, 50, " · ".join(built) + f" · longest_match_{impl} replayed at every position of a "
             "window with every position hashed", 12, INK_SOFT)

    # The change per data type and level, every bar on one symmetric scale
    cells = {t: {} for t in types}
    for t in types:
        for l in levels:
            ref, v = rows[0][t].get(l), rows[1][t].get(l)
            if ref and v:
                d = change(v["time"], ref["time"])
                band = 100.0 * max(noise(ref, v), ref["spread"], v["spread"])
                cells[t][l] = (d, band, abs(d) > band, fmt_pair(ref["time"], v["time"]),
                               f"{t} level {l} - {fmt_pair(ref['time'], v['time'])} per position, "
                               f"{fmt_pct(round(d, 1))}, noise band ±{band:.1f}%")
    scale = nice(max(abs(c[0]) for row in cells.values() for c in row.values()) * 1.05)

    svg.text(16, 76, "Each panel sets time per position across, on one log scale, against average "
             "match length in bytes up from zero, up and to the left is better. The two runs land "
             "on the same spots", 11, INK_SOFT)
    svg.text(16, 91, f"at that scale, so the bars beneath give the change in time per position at "
             f"each level, {names[1]} against {names[0]}, every bar on one scale of ±{scale:g}%.",
             11, INK_SOFT)
    kinds = {l: next(rows[0][t][l]["slow"] for t in types if l in rows[0][t]) for l in levels}
    plain = [str(l) for l in levels if not kinds[l]]
    slow = [str(l) for l in levels if kinds[l]]
    rolling = [str(l) for l in levels if kinds[l] == "roll"]
    kx, ky = 16, 114
    for i in (0, 1):
        mark(svg, i, kx + 7, ky - 4)
        svg.text(kx + 20, ky, names[i], 11, INK)
        kx += 20 + text_width(names[i], 11) + 24
    for group, dashed, meaning in (
            (plain, False, SEARCHES[None]),
            (slow, True, SEARCHES["knuth"] + (f", {' and '.join(rolling)} on the rolling hash"
                                              if rolling else ""))):
        if not group:
            continue
        ladder_line(svg, 1, [(kx, ky - 4), (kx + 26, ky - 4)], dashed)
        text = f"level{'s' if len(group) > 1 else ''} {', '.join(group)}, the {meaning}"
        svg.text(kx + 34, ky, text, 11, INK)
        kx += 34 + text_width(text, 11) + 24
    kx, ky = 16, 134
    for hue, meaning in ((LESS, f"faster in {names[1]}"), (MORE, f"slower in {names[1]}"),
                         (NEUTRAL, "inside the noise")):
        svg.add(f'<rect x="{kx}" y="{ky - 9}" width="22" height="{BAR_H}" rx="2" fill="{hue}"/>')
        svg.text(kx + 28, ky, meaning, 11, INK)
        kx += 28 + text_width(meaning, 11) + 22
    svg.add(f'<rect x="{kx}" y="{ky - 11}" width="22" height="{STRIP_ROW - 2}" fill="{GRID}"/>')
    svg.text(kx + 28, ky, f"noise band, {NOISE_FLOOR:g}% or {NOISE_CVS} times the repetition cv"
             + (", or the gap between a row's two fastest passes" if passes > 1 else ""), 11, INK)

    # One time scale for every panel, so a ladder's reach compares across the data types
    times = [p["time"] for run in rows for t in types for p in run[t].values()]
    xlo, xhi = min(times) / 1.6, max(times) * 1.6
    top = 156
    cw = (WIDTH - 32 - (COLS - 1) * GAP_X) / COLS
    card_h = PANEL_H + 16 + len(levels) * STRIP_ROW + 14
    for k, t in enumerate(types):
        x0, y0 = 16 + (k % COLS) * (cw + GAP_X), top + (k // COLS) * (card_h + GAP_Y)
        ladders = {i: {l: rows[i][t][l] for l in levels if l in rows[i][t]} for i in (0, 1)}
        panel(svg, x0, y0, cw, t, levels, ladders, names, xlo, xhi)
        strip(svg, x0 + PAD_L, y0 + PANEL_H + 16, levels, cells[t], scale)
    y = top + ((len(types) + COLS - 1) // COLS) * (card_h + GAP_Y)

    # The typical data type per level. A median, one extreme input must not speak for the rest.
    typical = {}
    for l in levels:
        changes = [cells[t][l][0] for t in types if l in cells[t]]
        if len(changes) > 1:
            d = statistics.median(changes)
            typical[l] = (d, NOISE_FLOOR, abs(d) > NOISE_FLOOR, None,
                          f"median of {len(changes)} data types level {l} - "
                          f"{fmt_pct(round(d, 1))}")
    notes_x, bottom = 16 + PAD_L, y
    if typical:
        svg.text(16 + PAD_L, y + 4, f"median of the {len(types)} data types", 12, INK, weight="bold")
        bottom = strip(svg, 16 + PAD_L, y + 26, levels, typical, scale)
        notes_x = 16 + cw + GAP_X + PAD_L

    # Match lengths are deterministic, any difference is real, the ones that show are named
    moved = sorted(((rows[1][t][l]["len"] - rows[0][t][l]["len"], t, l)
                    for t in types for l in cells[t]
                    if rows[1][t][l]["len"] != rows[0][t][l]["len"]), key=lambda m: -abs(m[0]))
    total = sum(len(row) for row in cells.values())
    found = f"The average match length differs in {len(moved)} of {total} rows, "
    named = [f"{t} level {l} by {d:+.3f} bytes" for d, t, l in moved[:3] if abs(d) >= 0.0005]
    rest = len(moved) - len(named)
    if not moved:
        found = f"The average match length is the same in all {total} rows."
    elif not named:
        found += "all by under 0.0005 bytes."
    else:
        found += ", ".join(named + ([f"the other {rest} by less"] if rest else [])) + "."
    notes = wrap(found, 10.5, WIDTH - 16 - notes_x) + [""] + wrap(
        "The benchmark hashes every position before the replay, so its chains run longer than "
        "deflate's own insert policy leaves them and the times overstate the cost inside deflate.",
        10.5, WIDTH - 16 - notes_x)
    for n, line in enumerate(notes):
        svg.text(notes_x, y + 4 + n * 15, line, 10.5, INK_SOFT)
    bottom = max(bottom, y + 4 + len(notes) * 15)
    height = footnote(svg, names, versions, machine, warnings, bottom)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg.finish(height))


def print_table(names, types, levels, rows):
    width = max(len(t) for t in types) + 2
    print(f"{'input':<{width}}{'level':>5}{names[0] + ' ns':>16}{names[1] + ' ns':>16}"
          f"{'change':>9}{names[0] + ' len':>17}{names[1] + ' len':>17}")
    for t in types:
        for l in levels:
            ref, v = rows[0][t].get(l), rows[1][t].get(l)
            if ref and v:
                print(f"{t:<{width}}{l:>5}{ref['time']:>16.2f}{v['time']:>16.2f}"
                      f"{fmt_pct(round(change(v['time'], ref['time']), 1)):>9}{ref['len']:>17.5f}"
                      f"{v['len']:>17.5f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsons", nargs="*",
                    help="two benchmark JSON outputs, the reference run first, each one pass or "
                         "several joined by commas")
    ap.add_argument("-o", "--output", default=None, help="output SVG path, JSON with --measure")
    ap.add_argument("--filter", default=None, help="regex applied to data type names")
    ap.add_argument("--names", default=None, help="comma-separated run names")
    ap.add_argument("--title", default=None, help="chart title")
    ap.add_argument("--impl", default=None,
                    help="implementation to chart, such as neon or avx2, the last both runs have "
                         "by default")
    ap.add_argument("--measure", default=None, metavar="REF",
                    help="build and run one zlib-ng revision instead, pr/<number> for a PR head")
    ap.add_argument("--reps", type=int, default=5, help="repetitions for --measure")
    ap.add_argument("--workdir", default=str(ROOT / ".pr-bench"))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.measure:
        return measure(args)
    if len(args.jsons) != 2:
        ap.error("give two benchmark JSON outputs, or --measure with a revision")

    sides = [[load_run(p) for p in arg.split(",")] for arg in args.jsons]
    for arg, side in zip(args.jsons, sides):
        if len({run[1] for run in side}) > 1:
            ap.error(f"the passes in {arg} come from different builds")
    # The first pass of a side names it and its machine
    runs = [side[0] for side in sides]
    names = [r[0] for r in runs]
    # A/B runs share the benchmark_zlib binary name, their files tell them apart
    if len(set(names)) < len(names):
        names = [os.path.splitext(os.path.basename(arg.split(",")[0]))[0] for arg in args.jsons]
    if args.names:
        for i, given in enumerate(args.names.split(",")[:len(names)]):
            if given:
                names[i] = given
    versions = [r[1] for r in runs]
    machine = machine_line([r[3] for r in runs])

    found = [fastest([match_rows(run[2]) for run in side]) for side in sides]
    impls = [i for i in found[0] if i in found[1]]
    impl = args.impl or (impls[-1] if impls else None)
    if impl not in impls:
        print(f"No longest_match rows for {impl or 'any implementation'} in both runs, "
              f"they share {', '.join(impls) or 'none'}.", file=sys.stderr)
        sys.exit(1)
    rows = [f[impl] for f in found]
    flt = re.compile(args.filter) if args.filter else None
    types = [t for t in order_types(set(rows[0]) & set(rows[1]))
             if (not flt or flt.search(t)) and set(rows[0][t]) & set(rows[1][t])]
    if not types:
        print("No data types in common.", file=sys.stderr)
        sys.exit(1)
    levels = sorted({l for t in types for l in set(rows[0][t]) & set(rows[1][t])})

    # The cv warning counts the rows charted, whichever pass each came from
    charted = [(r[0], r[1], {(t, l): {"_cv": row["cv"]} for t in types
                             for l, row in rows[i][t].items()}, r[3]) for i, r in enumerate(runs)]
    warnings = run_warnings(names, charted)
    title = args.title or f"{names[1]} against {names[0]}, longest_match by data type"
    out_path = args.output or re.sub(r"[^\w.-]+", "_", "_vs_".join(names)) + "_longest_match.svg"
    for i in range(len(runs)):
        if versions[i]:
            print(f"{names[i]} {versions[i]}")
    for w in warnings:
        print(f"warning: {w}")
    print(f"longest_match_{impl}, ns per position and average match length")
    print_table(names, types, levels, rows)
    render(names, versions, machine, warnings, impl, types, levels, rows,
           max(len(side) for side in sides), title, out_path)
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    sys.exit(main())
