#!/usr/bin/env python3
"""Map which input positions a zlib-ng build inserts into its hash table.

Builds zlib-ng twice, a base and a head revision, with every hash-table
store traced to an absolute input offset, compresses the input once per
build at one level, and renders the share of inserted positions per pixel
as a raster: base, head and head minus base side by side. A tar input gets
its member boundaries marked with the per-file insert share, or with
--per-file each member becomes its own block at its own scale. Made to show
where a hashing change spends its inserts, which the size and time grids
only sum up.

The traced builds are regular codecbench builds of codecbench_zlibng with
INSERT_TRACE_ENABLED defined, which the script patches into a worktree per
revision under --workdir (trace-<name>, build-trace-<name>). The patch
hooks the head-table stores in insert_string_p.h and the window slide in
deflate.c, so it follows zlib-ng's source and fails loudly if either moves.
Traced builds produce the same output as untraced ones.

Usage:
    scripts/insert_map.py <input> --pr <number> --level 6 -o out.svg
    scripts/insert_map.py <input> --base <ref> --head <ref> [--level 6]
        [--names a,b] [--title text] [--pixel bytes] [--cols n] [--scale n]
        [--per-file [--rows n]]
        [--workdir .pr-bench] -o out.svg

The per-file shares are also printed to stdout.
"""
import argparse
import base64
import math
import re
import struct
import subprocess
import sys
import zlib
from pathlib import Path

from graph_runs import GRID, INK, INK_SOFT, REPO_URL, Svg

UPSTREAM = "https://github.com/zlib-ng/zlib-ng.git"
SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
BACKEND_OFF = ["WITH_LIBDEFLATE", "WITH_ISAL", "WITH_SLZ", "WITH_MINIZ",
               "WITH_CHROMIUM_ZLIB", "WITH_MADLER_ZLIB", "WITH_ZLIB_RS",
               "WITH_LIBCOMPRESSION"]

# One hue light to dark for the share inserted, two hues around a neutral
# midpoint for the difference. The rasters are PNGs, so they keep these
# colors in dark mode, the labels around them follow the page theme.
SEQ_LIGHT, SEQ_DEEP = (244, 247, 251), (15, 63, 122)
DIV_LESS, DIV_MID, DIV_MORE = (42, 120, 214), (236, 235, 232), (235, 104, 52)
DIFF_SPAN = 0.25  # the diverging ramp saturates at this many points of share

TRACE_MARK = "INSERT_TRACE_ENABLED"

HARNESS = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "zlib-ng.h"
extern uint8_t *zng_trace_bits;
extern uint64_t zng_trace_len;
extern uint64_t zng_trace_base;
int main(int argc, char **argv) {
    if (argc != 4) return 1;
    FILE *f = fopen(argv[1], "rb");
    if (!f) return 1;
    fseek(f, 0, SEEK_END);
    size_t n = (size_t)ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *in = (uint8_t *)malloc(n);
    if (fread(in, 1, n, f) != n) return 2;
    fclose(f);
    int level = atoi(argv[2]);
    size_t nbytes = (n + 7) / 8;
    zng_trace_bits = (uint8_t *)calloc(nbytes, 1);
    zng_trace_len = n;
    zng_trace_base = 0;
    size_t cap = zng_deflateBound(NULL, n) + 1024;
    uint8_t *out = (uint8_t *)malloc(cap);
    zng_stream strm;
    memset(&strm, 0, sizeof(strm));
    if (zng_deflateInit2(&strm, level, Z_DEFLATED, -15, 9, Z_DEFAULT_STRATEGY) != Z_OK) return 3;
    strm.next_in = in;
    strm.avail_in = (uint32_t)n;
    strm.next_out = out;
    strm.avail_out = (uint32_t)cap;
    if (zng_deflate(&strm, Z_FINISH) != Z_STREAM_END) return 4;
    uint64_t set = 0;
    for (size_t i = 0; i < nbytes; i++) set += __builtin_popcount(zng_trace_bits[i]);
    FILE *o = fopen(argv[3], "wb");
    if (!o) return 5;
    fwrite(zng_trace_bits, 1, nbytes, o);
    fclose(o);
    printf("%zu %lu %llu\n", n, (unsigned long)strm.total_out, (unsigned long long)set);
    return 0;
}
'''

TRACE_DEFS = '''
#ifdef INSERT_TRACE_ENABLED
uint8_t *zng_trace_bits = NULL;
uint64_t zng_trace_len = 0;
uint64_t zng_trace_base = 0;
void zng_insert_trace(uint32_t idx) {
    uint64_t p = zng_trace_base + idx;
    if (p < zng_trace_len)
        zng_trace_bits[p >> 3] |= (uint8_t)(1u << (p & 7));
}
void zng_insert_trace_slide(uint32_t w) {
    zng_trace_base += w;
}
#endif
'''


def sh(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    subprocess.run(cmd, check=True, **kw)


def out(cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True,
                          **kw).stdout.strip()


def resolve(workdir, args):
    """Return (base rev, head rev, names) from --pr or --base/--head."""
    repo = workdir / "zlib-ng"
    if not repo.exists():
        sh(["git", "clone", UPSTREAM, str(repo)])
    if args.pr:
        sh(["git", "-C", str(repo), "fetch", "origin", "develop",
            f"+pull/{args.pr}/head:refs/pr/{args.pr}"])
        head = out(["git", "-C", str(repo), "rev-parse", f"refs/pr/{args.pr}"])
        base = out(["git", "-C", str(repo), "merge-base", "origin/develop", head])
        names = ["base", f"pr-{args.pr}"]
        labels = args.names.split(",") if args.names else ["PR base", "PR head"]
    else:
        if not (args.base and args.head):
            sys.exit("give --pr or both --base and --head")
        sh(["git", "-C", str(repo), "fetch", "origin", "develop"])
        base = out(["git", "-C", str(repo), "rev-parse", args.base])
        head = out(["git", "-C", str(repo), "rev-parse", args.head])
        names = [base[:8], head[:8]]
        labels = args.names.split(",") if args.names else [args.base, args.head]
    if len(labels) != 2:
        sys.exit("--names takes two comma-separated names")
    return repo, base, head, names, labels


def worktree(repo, workdir, name, rev):
    """A detached worktree per revision, reset so the trace patch applies cleanly."""
    tree = workdir / f"trace-{name}"
    if not tree.exists():
        sh(["git", "-C", str(repo), "worktree", "add", "--detach", str(tree), rev])
    else:
        sh(["git", "-C", str(tree), "checkout", "-q", "--force", "--detach", rev])
    return tree


def instrument(tree):
    """Hook every head-table store and the window slide, once."""
    p = tree / "insert_string_p.h"
    s = p.read_text()
    if TRACE_MARK in s:
        return
    anchor = "#define KNUTH_SHIFT (32 - HASH_BITS)\n"
    if s.count(anchor) != 1:
        sys.exit(f"{p}: expected one KNUTH_SHIFT define to hang the trace macro on")
    s = s.replace(anchor, anchor + f'''
/* Trace every hash table insert to an absolute input offset */
#ifdef {TRACE_MARK}
void zng_insert_trace(uint32_t idx);
#  define INSERT_TRACE(idx) zng_insert_trace(idx)
#else
#  define INSERT_TRACE(idx) ((void)0)
#endif
''')
    lines, hooked = [], 0
    for line in s.splitlines(keepends=True):
        m = re.match(r"^(\s*)(s->head|headp)\[h\] = \(Pos\)(\w+);", line)
        if m:
            lines.append(f"{m.group(1)}INSERT_TRACE({m.group(3)});\n")
            hooked += 1
        lines.append(line)
    if hooked == 0:
        sys.exit(f"{p}: no head-table stores found to hook")
    p.write_text("".join(lines))

    p = tree / "deflate.c"
    s = p.read_text()
    slide = re.findall(r"^(\s*)s->strstart\s*-= wsize;.*\n", s, flags=re.M)
    if len(slide) != 1:
        sys.exit(f"{p}: expected one window slide of strstart to hook")
    s = re.sub(r"^(\s*)(s->strstart\s*-= wsize;.*\n)",
               lambda m: m.group(0) + f"#ifdef {TRACE_MARK}\n{m.group(1)}"
               f"zng_insert_trace_slide(wsize);\n#endif\n", s, count=1, flags=re.M)
    s = s.replace('#include "deflate_p.h"\n',
                  f'#include "deflate_p.h"\n#ifdef {TRACE_MARK}\n'
                  'void zng_insert_trace_slide(uint32_t w);\n#endif\n', 1)
    p.write_text(s)

    p = tree / "insert_string.c"
    p.write_text(p.read_text() + TRACE_DEFS)
    print(f"{tree.name}: hooked {hooked} insert sites", file=sys.stderr)


def build(workdir, name, tree):
    """A codecbench build of the traced tree, the static library is what we need."""
    build_dir = workdir / f"build-trace-{name}"
    flags = [f"-D{opt}=OFF" for opt in BACKEND_OFF]
    sh(["cmake", "-B", str(build_dir), "-S", str(ROOT),
        f"-DZLIBNG_SOURCE_DIR={tree}", f"-DCMAKE_C_FLAGS=-D{TRACE_MARK}"] + flags,
       stdout=subprocess.DEVNULL)
    sh(["cmake", "--build", str(build_dir), "-j", "--target", "codecbench_zlibng"],
       stdout=subprocess.DEVNULL)
    lib_dir = build_dir / "_deps" / "zlib_ng-build"
    return lib_dir / "libz-ng.a", lib_dir


def harness(workdir, name, lib, inc):
    hdir = workdir / "insert-trace"
    hdir.mkdir(exist_ok=True)
    src = hdir / "trace.c"
    src.write_text(HARNESS)
    exe = hdir / f"trace-{name}"
    sh(["cc", "-O2", f"-I{inc}", str(src), str(lib), "-o", str(exe)])
    return exe


def trace(exe, input_path, level, bits_path):
    line = out([str(exe), str(input_path), str(level), str(bits_path)])
    n, compressed, inserted = (int(v) for v in line.split())
    return n, compressed, inserted


def tar_members(data):
    """(name, offset, size) per regular member, or one region for a plain file."""
    if len(data) < 512 or data[257:262] != b"ustar":
        return []
    members, off = [], 0
    while off + 512 <= len(data):
        hdr = data[off:off + 512]
        if hdr == b"\0" * 512:
            break
        name = hdr[:100].split(b"\0", 1)[0].decode(errors="replace")
        size = int(hdr[124:136].split(b"\0", 1)[0].strip() or b"0", 8)
        if hdr[156:157] in (b"0", b"\0"):
            members.append((name.split("/")[-1], off + 512, size))
        off += 512 + ((size + 511) // 512) * 512
    return members


def densities(bits, start, size, pixel):
    """Share of inserted positions per pixel over [start, start + size)."""
    dens = []
    for p in range(start, start + size, pixel):
        span = min(pixel, start + size - p)
        chunk = bits[p >> 3:(p + span + 7) >> 3]
        v = int.from_bytes(chunk, "little") >> (p & 7)
        dens.append((v & ((1 << span) - 1)).bit_count() / span)
    return dens


def share(dens):
    return 100 * sum(dens) / len(dens)


def pixel_for(size, cols, rows):
    """Input bytes per pixel, a power of two that fills about rows rows."""
    return max(64, 1 << round(math.log2(max(1, size / (cols * rows)))))


def lerp(c0, c1, t):
    return tuple(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))


def seq(v):
    return lerp(SEQ_LIGHT, SEQ_DEEP, max(0.0, min(1.0, v)))


def div(v):
    v = max(-1.0, min(1.0, v))
    return lerp(DIV_MID, DIV_MORE, v) if v >= 0 else lerp(DIV_MID, DIV_LESS, -v)


def hexcolor(c):
    return "#%02x%02x%02x" % c


def png(pixels, cols, rows):
    """An RGBA PNG from a row-major pixel list, the unfilled tail transparent."""
    raw = bytearray()
    for r in range(rows):
        raw.append(0)
        for c in range(cols):
            i = r * cols + c
            raw += bytes(pixels[i]) + b"\xff" if i < len(pixels) else b"\0\0\0\0"

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", cols, rows, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


def fmt_bytes(n):
    if n % 1048576 == 0:
        return f"{n // 1048576} MiB"
    if n % 1024 == 0:
        return f"{n // 1024} KiB"
    return f"{n:,} bytes"


def panel(svg, x, y, pix, cols, rows, scale):
    data = base64.b64encode(png(pix, cols, rows)).decode()
    svg.add(f'<image x="{x}" y="{y}" width="{cols * scale}" height="{rows * scale}" '
            f'style="image-rendering:pixelated" href="data:image/png;base64,{data}"/>')
    svg.add(f'<rect x="{x - 0.5}" y="{y - 0.5}" width="{cols * scale + 1}" '
            f'height="{rows * scale + 1}" fill="none" stroke="{GRID}" stroke-width="1"/>')


def legends(svg, left, dx, ly, labels):
    for i in range(64):
        svg.add(f'<rect x="{left + i * 5}" y="{ly}" width="5" height="10" '
                f'fill="{hexcolor(seq(i / 63))}"/>')
    svg.text(left, ly + 24, "0% inserted", 10.5, INK_SOFT)
    svg.text(left + 320, ly + 24, "100%", 10.5, INK_SOFT, anchor="end")
    for i in range(64):
        svg.add(f'<rect x="{dx + i * 5}" y="{ly}" width="5" height="10" '
                f'fill="{hexcolor(div(-1 + 2 * i / 63))}"/>')
    svg.text(dx, ly + 24, f"-{DIFF_SPAN * 100:.0f} points, fewer in {labels[1]}", 10.5, INK_SOFT)
    svg.text(dx + 320, ly + 24, f"+{DIFF_SPAN * 100:.0f}, more in {labels[1]}", 10.5, INK_SOFT,
             anchor="end")


def triplet(base, head):
    """Pixel colors for the base, head and difference panels of one region."""
    return ([seq(v) for v in base], [seq(v) for v in head],
            [div((h - b) / DIFF_SPAN) for b, h in zip(base, head)])


def render(args, input_path, level, labels, revs, bits_paths, n, totals, members):
    cols, scale, gap = args.cols, args.scale, 28
    left, top = 150, 152
    panel_w = cols * scale
    width = left + 3 * panel_w + 2 * gap + 24
    bits = [Path(p).read_bytes() for p in bits_paths]
    share_b, share_h = 100 * totals[0] / n, 100 * totals[1] / n
    regions = members or [(Path(input_path).name, 0, n)]
    per_file = args.per_file and len(regions) > 1

    svg = Svg(width)
    what = f"zlib-ng PR #{args.pr}" if args.pr else "zlib-ng"
    title = args.title or f"{what} hash inserts over {Path(input_path).name}, level {level}"
    svg.text(24, 36, title, 20, INK, weight="bold")
    legends(svg, left, left + 2 * (panel_w + gap), 76, labels)
    heads = [labels[0], labels[1], f"{labels[1]} minus {labels[0]}"]
    subs = [f"{share_b:.1f}% of positions inserted", f"{share_h:.1f}% of positions inserted",
            f"{share_h - share_b:+.1f} points overall, ramp saturates at ±{DIFF_SPAN * 100:.0f}"]
    table = []

    if per_file:
        rows_target = args.rows
        svg.text(24, 58, f"{labels[0]} {revs[0][:8]} · {labels[1]} {revs[1][:8]} · every file "
                 f"laid out at its own scale, {cols} pixels per row, brightness is the "
                 "share of positions inserted into the hash table", 12.5, INK_SOFT)
        for k in range(3):
            x = left + k * (panel_w + gap)
            svg.text(x, top - 26, heads[k], 13, INK, weight="bold")
            svg.text(x, top - 10, subs[k], 11.5, INK_SOFT)
        y = top
        for name, start, size in regions:
            pixel = pixel_for(size, cols, rows_target)
            base = densities(bits[0], start, size, pixel)
            head = densities(bits[1], start, size, pixel)
            rows = (len(base) + cols - 1) // cols
            for k, pix in enumerate(triplet(base, head)):
                panel(svg, left + k * (panel_w + gap), y, pix, cols, rows, scale)
            svg.text(left - 10, y + 12, name, 12, INK, anchor="end", weight="bold")
            svg.text(left - 10, y + 27, f"{share(base):.0f}% → {share(head):.0f}%", 11, INK_SOFT,
                     anchor="end")
            svg.text(left - 10, y + 41, f"{size / 1e6:.1f} MB, {fmt_bytes(pixel)} per pixel", 10,
                     INK_SOFT, anchor="end")
            table.append((name, size, share(base), share(head)))
            y += rows * scale + 18
        bottom = y - 18
    else:
        pixel = args.pixel or pixel_for(n, cols, 400)
        base = densities(bits[0], 0, n, pixel)
        head = densities(bits[1], 0, n, pixel)
        rows = (len(base) + cols - 1) // cols
        panel_h = rows * scale
        svg.text(24, 58, f"{labels[0]} {revs[0][:8]} · {labels[1]} {revs[1][:8]} · one pixel is "
                 f"{fmt_bytes(pixel)} of input, {fmt_bytes(pixel * cols)} per row, brightness is "
                 "the share of positions inserted into the hash table", 12.5, INK_SOFT)
        for k, pix in enumerate(triplet(base, head)):
            x = left + k * (panel_w + gap)
            svg.text(x, top - 26, heads[k], 13, INK, weight="bold")
            svg.text(x, top - 10, subs[k], 11.5, INK_SOFT)
            panel(svg, x, top, pix, cols, rows, scale)
        for name, start, size in regions:
            yb = top + (start / pixel / cols) * scale
            if start:
                for k in range(3):
                    x = left + k * (panel_w + gap)
                    svg.line(x, yb, x + panel_w, yb, GRID, 1)
            a, b = start // pixel, max(start // pixel + 1, (start + size) // pixel)
            sb, sh = share(base[a:b]), share(head[a:b])
            mid = top + ((start + size / 2) / pixel / cols) * scale
            svg.text(left - 10, mid + 4, f"{name}  {sb:.0f}% → {sh:.0f}%", 11, INK, anchor="end")
            table.append((name, size, sb, sh))
        bottom = top + panel_h
        print(f"{Path(input_path).name}, level {level}, {fmt_bytes(pixel)} per pixel")

    height = bottom + 40
    svg.text(24, height - 12, "raw deflate, memLevel 9, one-shot, traced builds reproduce the "
             f"untraced output · {REPO_URL.removeprefix('https://')}", 10.5, INK_SOFT)
    Path(args.output).write_text(svg.finish(height))

    print(f"{'region':14s} {'size':>10s}  {labels[0]:>10s}  {labels[1]:>10s}")
    for name, size, sb, sh in table:
        print(f"{name:14s} {size / 1e6:8.1f} MB  {sb:9.1f}%  {sh:9.1f}%")
    print(f"{'all':14s} {n / 1e6:8.1f} MB  {share_b:9.1f}%  {share_h:9.1f}%")
    print(f"wrote {args.output}")

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="file to compress, a tar gets its members marked")
    ap.add_argument("--pr", type=int, help="zlib-ng/zlib-ng pull request, base is its merge-base")
    ap.add_argument("--base", help="base revision when not using --pr")
    ap.add_argument("--head", help="head revision when not using --pr")
    ap.add_argument("--level", type=int, default=6)
    ap.add_argument("--names", help="comma-separated panel names")
    ap.add_argument("--title", help="chart title")
    ap.add_argument("--pixel", type=int, help="input bytes per pixel, default fits about 400 rows")
    ap.add_argument("--cols", type=int, default=256, help="pixels per row")
    ap.add_argument("--scale", type=int, default=2, help="screen pixels per map pixel")
    ap.add_argument("--per-file", action="store_true",
                    help="lay out each tar member as its own block at its own scale")
    ap.add_argument("--rows", type=int, default=32,
                    help="rows per member with --per-file, sets its pixel size")
    ap.add_argument("--workdir", default=str(ROOT / ".pr-bench"))
    ap.add_argument("-o", "--output", required=True, help="output SVG path")
    args = ap.parse_args()

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    input_path = Path(args.input).resolve()
    if not input_path.is_file():
        sys.exit(f"{input_path}: not a file")

    repo, base, head, names, labels = resolve(workdir, args)
    revs = [base, head]
    bits, totals, n = [], [], None
    for name, rev in zip(names, revs):
        print(f"{name}: {out(['git', '-C', str(repo), 'log', '-1', '--format=%h %s', rev])}")
        tree = worktree(repo, workdir, name, rev)
        instrument(tree)
        lib, inc = build(workdir, name, tree)
        exe = harness(workdir, name, lib, inc)
        bits_path = workdir / "insert-trace" / f"{name}-{input_path.stem}-l{args.level}.bits"
        size, compressed, inserted = trace(exe, input_path, args.level, bits_path)
        print(f"{name}: {size:,} bytes in, {compressed:,} out, {inserted:,} inserted "
              f"({100 * inserted / size:.1f}%)")
        bits.append(bits_path)
        totals.append(inserted)
        n = size

    members = tar_members(input_path.read_bytes())
    render(args, input_path, args.level, labels, revs, bits, n, totals, members)
    return 0


if __name__ == "__main__":
    sys.exit(main())
