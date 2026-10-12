## Project Basics

- Use CMake build system.
- Every backend is fetched at a pinned version during configure; override with
  the `<NAME>_REPOSITORY` / `<NAME>_TAG` variables.
- Binaries are named `codecbench_<backend>` and land in the build directory
  root. Backends: zlibng (reference), libdeflate, isal, slz, chromium_zlib,
  madler_zlib, zlib_rs, miniz, libcompression (macOS only).
- All binaries register identical benchmark names over the shared 0-9 level
  ladder, so their JSON outputs compare directly. libdeflate continues
  through `level:12`, miniz through `level:10`, igzip spans levels 0-3,
  libslz has a single level and registers no inflate benchmarks,
  libcompression's single fixed quality registers as `level:5`.
- Deflate strategy variants (`/strategy:<name>`) register for zlib-ng, the
  stock zlib API backends (chromium_zlib, madler_zlib, zlib_rs), and miniz.
  They cover levels 1, 6 and 9 (7 and 9 for `filtered`), except `fixed`,
  which covers levels 1-9.
- windowBits variants (`/level:6/wbits:<9-15>`) register for zlib-ng and the
  stock zlib API backends only.
- Checksum benchmarks (`codec_crc32/size:<n>`, `codec_adler32/size:<n>`)
  register a 64 byte to 2 MiB size ladder for every backend except
  libcompression, results verified against zlib-ng.
- Decompression input is always produced by zlib-ng at level 9 and all output
  is verified against the original data, a failed roundtrip shows up as a
  benchmark error.
- The zstd backend is format context, not a deflate implementation: compress
  only, a thinned level 1-19 ladder, streams verified through zstd itself.

## Test Data

- Clone the corpora repo before running:
  `git clone https://github.com/zlib-ng/corpora test/data/corpora`.
- Synthetic inputs are selected with `--benchmark_data_types=<type,...|all>`
  (text, short_match, dna, random, literals, mixed, realistic_rgb,
  striped_rgb, phased, runs, far_match, records, dist). `dist` is strictly periodic
  input, deflate at every level for periods 1-16, 32, 64, 128 and 258, inflate for
  39 distances up to 258. Each other type registers deflate
  per level plus inflate at 128 KiB, and a DRAM-resident
  `codec_inflate/data/<type>/size:8388608` variant, the in-cache and 8 MiB
  runs rank the backends differently. The generators are vendored in `test_data.h` so input
  definitions stay pinned to this repository.

## Benchmarking

- To benchmark zlib-ng work in progress, configure with
  `-D ZLIBNG_SOURCE_DIR=<checkout>`. The default builds the pinned release,
  keep separate build directories for release and work-in-progress builds.
- When running with `--benchmark_repetitions`, also use
  `--benchmark_report_aggregates_only=true` and `--benchmark_cooldown=5`.
- Run benchmark processes sequentially, otherwise contention causes
  unreliable results.
- Don't run benchmarks in the background.
- Keep each benchmark process under 10 minutes, longer commands get forced
  into the background. Split a full run into groups with
  `--benchmark_filter` (e.g. `level:1$` per level, `strategy:` for the
  strategy variants, `^codec_inflate` for inflate) and merge the JSON
  outputs afterwards with
  `jq -s '.[0] * {benchmarks: (map(.benchmarks) | add)}' part*.json`.
- Look for other benchmark processes running on the machine to avoid
  contamination and wait until they are done.
- Prefer `scripts/run_clean.py <binary> --filter <re> --out <file>` over raw
  benchmark invocations, it waits for an idle machine and automatically
  reruns rows whose real/cpu ratio or cv shows contamination.

### Result Layout

- Raw benchmark JSONs live under `results/raw/<segment>/<backend>.json`,
  one segment per corpus directory plus `synthetic` for the data-type and
  checksum rows, so any segment can be rerun without touching the others.
- Combine segments for graphing with
  `scripts/merge_runs.py results/raw/{tars,silesia,synthetic}/<backend>.json
  -o <backend>.json`, later files win on duplicate run names.
- The zlib-ng work-in-progress build's rows are stored as
  `zlibng-prs.json`, the pinned develop build's as `zlibng-develop.json`.

### Comparing Results

- Run with `--benchmark_out=<file>.json --benchmark_out_format=json` and
  compare with `scripts/compare_runs.py base.json contender.json`. It reports
  time, compression ratio, and byte deltas.
- For a per-level A/B of one codec, `scripts/compare_levels.py base.json
  head.json -o <file>.svg` charts the level ladder above a table of compressed
  size and cpu time per level. Strategy rows in the JSONs become their own
  lines and table.
- For where an A/B result depends on the data, `scripts/compare_inputs.py
  base.json head.json -o <file>.svg` grids the size and cpu time change per
  input and level, over corpus files and synthetic data types alike.
- For where a hashing change spends its inserts, `scripts/insert_map.py <input>
  --pr <n> --level <l> -o <file>.svg` traces every hash-table store in a base and
  head build and rasters the inserted share per 2 KiB, tar members marked.
- For whether a change beats the base's own level ladder, `scripts/frontier.py
  base.json head.json -o <file>.svg` draws speed against size per input and rings
  points the other run beats on both axes.
- For what the bits were spent on, `scripts/stream_anatomy.py base.json head.json
  --level <l> -o <file>.svg` stacks bits by literals, lengths, distances, stored and
  overhead per input with the match distance and length mix, from the `bits_*`,
  `dist_*` and `len_*` counters every deflate row carries. Sizes are deterministic,
  so a `--benchmark_min_time=0.05s` run is enough for it.
- For what each symbol costs, `scripts/symbol_cost.py base.json head.json --level <l>
  -o <file>.svg` charts bits per literal, bits per match as length plus distance, and
  bits per match byte across the inputs, from the same counters.
- For what the match search itself costs and finds, `scripts/longest_match.py base.json
  head.json -o <file>.svg` charts zlib-ng's own longest_match benchmark, a panel per data
  type of time per position against average match length, with the time change per level
  beneath it over its noise band. The JSONs come from `scripts/longest_match.py --measure
  <ref>`, which builds `benchmark_zlib` at one zlib-ng revision, `pr/<n>` for a PR head,
  and writes `.pr-bench/lm-<rev>.json`. Measure each side twice with `-o` and join the
  passes with commas, two processes of one binary differ by up to 4% on these rows.

### Presenting Results

- Always show performance changes as percentage (e.g. -18.4%), not as
  speedup ratios (e.g. 1.23x).
- When publishing results as a GitHub gist, start the title and filename with
  the project name and include the machine specs.

### Thermal Throttling

Benchmark results are vulnerable to thermal throttling, sustained workloads
heat the CPU until it downclocks and later benchmarks run slower than earlier
ones.

Signs of thermal contamination:

- Later benchmarks in a run are slower than earlier ones.
- The same benchmark gives wildly different results across runs.
- CV exceeds 3% on benchmarks that normally have less than 1%.

Mitigation:

- Add `sleep 10` or longer between separate benchmark processes.
- Run benchmark groups separately rather than all in one long chain.
- Verify with a quick A/B sanity check before committing to a full run.

See https://gist.github.com/nmoinvaz/42d997329fc4878993ec0f4f8e600c91 for
platform-specific steps to stabilize benchmark environments.
