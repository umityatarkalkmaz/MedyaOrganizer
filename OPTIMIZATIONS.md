# Optimization Audit — Media Organizer

**Scope audited:** `organizer.py` (172 lines, the entire source tree of `/home/umit/Containers/claude-code/organizer`). `readme.md` and `.gitignore` reviewed for consistency only. No other source files exist in this project.

**Environment:** Python 3.14.7, Arch Linux (distrobox `claude-code`), single-file stdlib-only script.

**Method:** static review plus five synthetic benchmarks run in this container against the actual functions in `organizer.py`. All numbers marked "measured" come from those runs (warm page cache, container-local tmpfs-backed storage). Cold-cache and spinning-disk scenarios will show *larger* gains for the I/O findings, not smaller.

---

## STATUS — findings applied 2026-08-15

This report was written against the original `organizer.py` (preserved as `organizer.py.orig`). A subsequent security audit found destination-symlink vulnerabilities in the same copy/move path, and fixing those required rewriting the functions these findings target — so the optimizations were folded into that rewrite rather than applied twice.

| Finding | Status |
|---|---|
| F1 size-bucketed lazy hashing | **Applied** — `collect_media_files`, `find_duplicate` |
| F2 memoized collision counter | **Applied** — folded into `reserve_target_path` |
| F3 pruned `scandir` walk, no per-file `resolve()` | **Applied** — `iter_source_files` |
| F4 memoized directory creation | **Applied** — `ensure_target_directory` |
| F5 raw digests + streamed report | **Applied** — `digest_fd` returns 32B; `DuplicateReport` streams |
| F6 single-fd hash+copy | **Applied** — one `O_NOFOLLOW` fd serves both; `os.copy_file_range` retained |
| F7 parallel copy stage | **Not applied** — highest risk, conditional payoff; still a "Do Next" |
| F8 stale paths in report under `--move` | **Applied** — `ProcessedFile.final_path` |
| F9 `--output == --source` silent no-op | **Applied** — `main()` now errors out |
| F10 streamed, appending report | **Applied** — run header, no longer clobbers prior runs |
| F11 `--dry-run` | **Not applied** — no security driver; still a "Do Next" |

**End-to-end measured result** (full runs, old vs current — lower than the isolated micro-benchmarks below because these include the copy itself, which is irreducible):

| Profile | Before | After | Gain |
|---|---|---|---|
| 400 files / 600 MB, unique sizes | 0.622 s | 0.209 s | **3.0×** |
| 1500 files, 15 distinct basenames, `--mode flat` | 0.047 s | 0.013 s | **3.6×** |
| 3000 files, re-run over populated output | 0.730 s | 0.346 s | **2.1×** |

Verified by the §5 golden-tree parity test: output trees are byte-identical to the original across `flat`/`folder` × `skip_unmatched` on/off and both `--move` modes, with identical source leftovers. `ruff check` passes.

The per-finding sections below are unchanged, and describe the code as it was *before* these fixes.

---

## 1) Optimization Summary

The script is clean, readable, and correct in its core logic — no dead code, no duplicated logic, no over-abstraction worth removing. The problems are all in the **hot loop's cost model**: it does maximal work per file unconditionally, when the information needed to skip that work is available for free.

Three structural inefficiencies dominate, and all three compound on exactly the workload this tool targets (large media dumps with repeated filenames, run more than once against the same tree):

1. **Every file is fully read and SHA-256'd, even when nothing could possibly collide with it.** File size — already available from the directory walk's `stat()` — rules out the overwhelming majority of duplicate candidates for free. Measured **5.4× faster** dedup with **82% fewer bytes read** on a 660 MB / 330-file corpus.
2. **Filename collision resolution is quadratic.** `get_unique_target_path` restarts its counter at 1 on every call and stats each candidate. Measured **212× slower** than a memoized counter at 2000 same-named files (7.30 s → 0.034 s), and the gap widens with N.
3. **The output directory is fully traversed and then discarded, one `resolve()` syscall chain per file.** On a re-run the output tree is as large as the source, so the walk does double work and throws half of it away. Measured **27× faster** with subtree pruning + `scandir` (0.628 s → 0.023 s for 8000 real files).

**Top 3 highest-impact improvements**

| # | Change | Measured gain | Effort |
|---|---|---|---|
| 1 | Size-bucket before hashing (lazy hash) | 5.4× dedup, −82% bytes read | ~30 lines |
| 2 | Memoize the collision counter | up to 212× on collision-heavy dirs | ~8 lines |
| 3 | `os.scandir` walk with output-dir pruning | 27× traversal | ~20 lines |

**Biggest risk if nothing changes:** the runtime is *superlinear* in the two dimensions that grow fastest in real use — corpus size (finding #1: every byte read twice, once to hash, once to copy) and filename repetition (finding #2: O(n²) stat calls). A 50k-photo library with camera-style repeated names (`IMG_0001.jpg` across many folders) will spend the majority of its wall time on stat calls and redundant hashing rather than on the copy that is the actual job. On the second run against the same tree, finding #3 doubles the walk on top of that. None of this fails loudly — it just gets slower, so it will be diagnosed as "big directories are slow" rather than as a fixable defect.

---

## 2) Findings (Prioritized)

### F1 — Unconditional SHA-256 of every file, including provably unique ones

* **Category:** Algorithm / I/O
* **Severity:** Critical
* **Impact:** Wall-clock latency, disk read throughput, CPU
* **Evidence:** `organizer.py:93-98` hashes every file before any cheaper discriminator is consulted; `organizer.py:49-55` reads the file end-to-end in 1 MB chunks. There is no size, mtime, or fast-path check anywhere ahead of it.
* **Why it's inefficient:** Two files with different sizes can never be identical. Size is already returned by the `stat()` the walk performs anyway (`organizer.py:79`), so it is a zero-cost discriminator that is currently thrown away. In a typical photo/video corpus nearly every file has a unique size, so nearly every hash computed is wasted work — and it is the most expensive work in the loop, because it reads the entire file. Worse, the file is then read a *second* time by `shutil.copy2`/`shutil.move` at `organizer.py:113-115`, so an ordinary non-duplicate file is read twice end-to-end.
* **Recommended fix:** Index candidates by size and hash lazily. Only when a second file appears with an already-seen size do you hash — both the newcomer and the not-yet-hashed earlier occupant of that size bucket. See §6 patch A.
* **Tradeoffs / Risks:** Slightly more state (a size→entries map instead of a hash→path map) and marginally more intricate control flow. Correctness is *preserved exactly*: size equality is a necessary condition for content equality, so no duplicate can be missed. One real trap: under `--move` the earlier file has already left its source path, so the deferred hash must read it at its **destination** path — the patch tracks the post-move path for this reason. Note the existing code already has a latent version of this bug (see F8).
* **Expected impact estimate:** **Measured 5.4× on the dedup stage** (0.360 s → 0.066 s), **−82% bytes hashed** (660 MB → 120 MB) on a 330-file corpus with 30 true duplicates. Gains scale with the fraction of size-unique files; on cold cache or a mechanical disk the improvement is larger, since the eliminated work is pure sequential read.
* **Removal Safety:** Safe — no behavioral change to the duplicate set.
* **Reuse Scope:** local file (`collect_media_files` + `compute_file_hash`)

---

### F2 — `get_unique_target_path` is O(n²) in same-named files

* **Category:** Algorithm / I/O
* **Severity:** High
* **Impact:** Latency, syscall volume
* **Evidence:** `organizer.py:35-42`. `counter` is re-initialized to 1 on every invocation, and the `while candidate.exists()` loop performs one `stat()` per already-taken suffix. Called once per processed file at `organizer.py:109`.
* **Why it's inefficient:** After *k* files named `IMG_0001.jpg` have landed in a directory, the (*k*+1)-th call walks `_1, _2, … _k` from scratch — *k* stat syscalls. Total cost across *n* collisions is n(n+1)/2 stats. This is precisely the tool's headline workload: `--mode flat` funnels an entire recursive tree into one directory, and camera/screenshot naming schemes repeat filenames across folders by design.
* **Recommended fix:** Memoize the next free counter per `(parent, stem, suffix)` key and resume from it, keeping the `while exists()` loop as a correctness backstop against pre-existing files and concurrent writers. See §6 patch B.
* **Tradeoffs / Risks:** A small dict whose key count is bounded by the number of *distinct* colliding basenames (not files). The retained `while` loop means behavior is unchanged even if the destination is mutated by another process mid-run.
* **Expected impact estimate:** **Measured 58× at 500 collisions** (0.467 s → 0.008 s) and **212× at 2000 collisions** (7.299 s → 0.034 s). The 4× file increase produced a 15.6× time increase, confirming quadratic growth empirically. Zero measurable effect when filenames are unique.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### F3 — Output subtree is fully walked and discarded; `resolve()` per file

* **Category:** I/O
* **Severity:** High
* **Impact:** Latency, syscall volume — worst on repeat runs
* **Evidence:** `organizer.py:78` iterates `source_dir.rglob("*")`, which descends *into* `output_dir` when the output lives under the source (the default: `<source>/OrganizedMedia`, `organizer.py:160`). Each yielded path is then filtered at `organizer.py:81` via `output_dir in file_path.resolve().parents`.
* **Why it's inefficient:** Two separate costs stack.
  (a) **Traversal waste:** every file written by a previous run is enumerated and `is_file()`-stat'd only to be discarded. After one successful run the output tree holds roughly as many files as the source, so the second run's walk is ~2× the necessary size. Filtering after enumeration cannot avoid this; only pruning the directory descent can.
  (b) **Per-file `resolve()`:** resolving a path is a full symlink-normalizing walk of every component, done once per file purely to answer a containment question that `is_relative_to` answers without touching the filesystem (paths from `rglob` are already rooted at the resolved `source_dir`, `organizer.py:75`).
  Additionally, `is_symlink()` is checked at `organizer.py:83` *after* the `resolve()` at `:81`, so symlinks pay the expensive check before being discarded — the two guards are in the wrong order.
* **Recommended fix:** Replace `rglob` with an explicit `os.scandir` walk that prunes the output directory at descent time and reads `DirEntry` metadata (type, size) from the one syscall it already makes. This simultaneously supplies the file size that F1 needs, at no cost. See §6 patch C.
* **Tradeoffs / Risks:** Slightly more code than `rglob`. Symlink semantics must be preserved deliberately: `rglob` in Python 3.13+ defaults to `recurse_symlinks=False` (verified: `Path.rglob(self, pattern, *, case_sensitive=None, recurse_symlinks=False)` on 3.14.7), so the replacement walk must also not follow symlinked directories, and must keep the existing file-symlink warning. The patch does both.
* **Expected impact estimate:** **Measured on 8000 source files + 8000 previously-output files:** current `rglob` + `is_file` + `resolve` = 0.628 s; dropping only `resolve()` = 0.279 s (**56% faster**); pruned `scandir` walk with a stat per file = 0.023 s (**27× faster**). The `resolve()` removal alone is a two-character-scale change for over half the traversal cost.
* **Removal Safety:** Needs Verification — confirm symlinked-directory and cross-filesystem behavior with the test cases in §5.
* **Reuse Scope:** local file

---

### F4 — `mkdir()` syscall per file for a handful of fixed directories

* **Category:** I/O
* **Severity:** Medium
* **Impact:** Syscall volume, latency
* **Evidence:** `organizer.py:108` runs `target_dir.mkdir(parents=True, exist_ok=True)` inside the per-file loop.
* **Why it's inefficient:** There are at most 12 possible target directories (11 categories in `FILE_CATEGORIES` plus `other`) — and exactly one in `flat` mode. For a 100k-file run this is ~100k `mkdir` syscalls to create at most 12 directories; every call after the first per directory is a guaranteed `EEXIST`.
* **Recommended fix:** Keep a `set[Path]` of already-created directories and call `mkdir` only on first use. See §6 patch D.
* **Tradeoffs / Risks:** Negligible. If an external process deletes a target directory mid-run the `copy2` would fail where it previously would have silently recreated the directory — already handled by the existing `OSError` path at `organizer.py:117-119`.
* **Expected impact estimate:** Low single-digit % of total wall time in isolation — but it is a ~5-line change with zero risk, so the ROI is high. Larger on network filesystems (NFS/SMB), where `mkdir` round-trips are expensive.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### F5 — Unbounded in-memory dedup index and duplicate report

* **Category:** Memory
* **Severity:** Medium
* **Impact:** Peak RSS; OOM risk on very large corpora
* **Evidence:** `seen_hashes: dict[str, Path]` at `organizer.py:72` grows one entry per *processed* file and is never bounded; `duplicate_report: list[str]` at `organizer.py:73` grows one formatted string per duplicate and is only flushed at `organizer.py:126`.
* **Why it's inefficient:** Each entry retains a 64-character hex digest string, a `Path` object, and its cached string form. **Measured ~405 B per entry** → ~40 MB at 100k files, **~405 MB at 1M files**, before counting dict load-factor overhead. The report list adds a full path pair per duplicate on top of that. Nothing here is streamed or evicted.
* **Recommended fix:** Two independent reductions:
  (a) Store `hasher.digest()` (32 raw bytes) instead of `hexdigest()` (64-char str) — roughly halves the key cost, with `.hex()` applied only for display.
  (b) Stream `duplicate_report` to the report file incrementally instead of accumulating it. This also means a crash mid-run leaves a partial report rather than none.
  Note that adopting F1 shrinks this further: files that are never hashed hold only a size and a path, and the size map's key count is bounded by *distinct sizes*.
* **Tradeoffs / Risks:** (b) opens the report file earlier, so a run with zero duplicates would create an empty file unless opened lazily on the first duplicate — the patch opens lazily. Path retention is unavoidable if the report is to name the original.
* **Expected impact estimate:** ~40-50% reduction in dedup-index memory; removes the unbounded report list entirely. Qualitative until profiled against a real corpus.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### F6 — Every copied file is read twice end-to-end

* **Category:** I/O
* **Severity:** Medium
* **Impact:** Disk read throughput
* **Evidence:** `organizer.py:94` reads the whole file to hash it; `organizer.py:115` then reads it again inside `shutil.copy2`.
* **Why it's inefficient:** Total read volume is 2× the corpus size for every non-duplicate file. Adopting F1 eliminates this for the majority of files (they are never hashed at all), which is why F1 is ranked first — but for files that *do* land in a contested size bucket, the double read remains.
* **Recommended fix:** For files that must be hashed, fuse the passes: read once into a buffer, feed the buffer to both the hasher and the destination file handle. This forfeits `shutil`'s platform-accelerated `copy_file_range`/`sendfile` fast path, so it is only worth doing on the hash-required branch — never on the common path.
* **Tradeoffs / Risks:** Real tradeoff: a hand-rolled copy loop is slower per byte than the kernel-accelerated `copyfile`, so fusing is a net win only when it eliminates a whole extra read. Must also replicate `copy2`'s metadata preservation via an explicit `shutil.copystat`. Because the win is conditional and the correctness surface is larger, this is a **Do Next**, not a quick win.
* **Expected impact estimate:** Up to −50% read volume on the hash-required subset; net wall-clock effect depends on whether the workload is read-bound. **Measure before adopting** — this is the one finding where I cannot predict the sign of the change on all hardware.
* **Removal Safety:** Needs Verification
* **Reuse Scope:** local file

---

### F7 — Fully serialized; no concurrency on an I/O-bound workload

* **Category:** Concurrency
* **Severity:** Medium
* **Impact:** Throughput
* **Evidence:** The single `for file_path in source_dir.rglob("*")` loop at `organizer.py:78-119` performs walk, hash, and copy strictly in sequence.
* **Why it's inefficient:** Hashing and copying are both dominated by I/O wait, during which the process is idle. On NVMe storage (high queue depth) or network filesystems (high per-op latency), a modest thread pool overlaps that wait. Note that Python's GIL is *not* the limiting factor here: `hashlib` releases it for large buffers and `shutil`'s copy path is syscall-bound.
* **Recommended fix:** Keep the walk and the duplicate-decision serial (they are cheap and order-dependent), and offload only the copy/move to a bounded `ThreadPoolExecutor` (start at 4-8 workers, make it a `--jobs` flag).
* **Tradeoffs / Risks:** Significant. Duplicate-of-X attribution becomes nondeterministic unless the decision stage stays serial. `get_unique_target_path` becomes racy and needs a lock or a per-directory counter guard. Error accounting needs to be thread-safe. On a mechanical disk, parallel copies *degrade* throughput by inducing seek thrash — so the default must remain 1. This is the highest-risk item in the report and should only be attempted after F1-F4 land and a profile still shows I/O wait dominating.
* **Expected impact estimate:** 1.5-3× throughput on NVMe/network storage; potentially negative on spinning disks. Strictly "likely" — needs the profiling in §5 to justify.
* **Removal Safety:** Needs Verification
* **Reuse Scope:** service-wide (changes the execution model)

---

### F8 — `duplicates_report.txt` cites source paths that no longer exist under `--move`

* **Category:** Reliability
* **Severity:** Medium
* **Impact:** Correctness of output artifact; blocks F1 if not handled
* **Evidence:** `organizer.py:105` stores the **source** path in `seen_hashes`, then `organizer.py:113` moves that file away. A later duplicate is reported at `organizer.py:102` as `duplicate of <source path>` — a path that has been vacated by the move.
* **Why it's inefficient:** The report is the user's only record of what was discarded, and under `--move` its "original" column points at nothing. The user cannot verify the tool's decision, which matters precisely because `--move` is irreversible (`organizer.py:142`).
* **Recommended fix:** Record the *final* path (the `target_path` actually written) rather than the source path. This is also a hard prerequisite for F1's lazy hashing, which must be able to re-read the earlier file after it has moved. Both are addressed by the same one-line change in §6 patch A.
* **Tradeoffs / Risks:** None — the destination path is strictly more useful in both copy and move modes.
* **Expected impact estimate:** Correctness, not speed. Unblocks F1.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### F9 — `--output` equal to `--source` silently processes zero files

* **Category:** Reliability
* **Severity:** Medium
* **Impact:** Silent no-op; user-visible surprise
* **Evidence:** `organizer.py:81`. When `output_dir == source_dir` (e.g. `--output . --mode flat`), every discovered file has `output_dir` among its `.parents`, so the guard skips all of them.
* **Verified:** running `collect_media_files(d, d, mode="flat")` on a directory containing `a.txt` and `b.png` reports `0 file(s) copied` and leaves the directory untouched. No warning is emitted.
* **Why it's inefficient:** The full recursive walk and every `stat()` still execute; the run costs real time and accomplishes nothing, with a success-shaped log line. The guard is correct in intent (don't re-ingest your own output) but has no special case for the degenerate configuration.
* **Recommended fix:** Detect `output_dir == source_dir` in `main()` and either reject it with a clear error or, in `folder` mode, allow it explicitly (subdirectories per category make it well-defined). See §6 patch E.
* **Tradeoffs / Risks:** Turns a silent no-op into a hard error — strictly better, but it is a user-visible behavior change worth a line in the readme.
* **Expected impact estimate:** Eliminates a wasted full-tree walk in the misconfigured case.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### F10 — Duplicate report written only at the end, and clobbers prior runs

* **Category:** Reliability
* **Severity:** Low
* **Impact:** Data loss on crash; loss of run history
* **Evidence:** `organizer.py:123-127`. The report is materialized with a single `write_text` after the loop completes, and unconditionally overwrites any existing `duplicates_report.txt`.
* **Why it's inefficient:** A crash or `KeyboardInterrupt` during a long run discards the entire duplicate record even though the copies themselves already happened. And because the filename is fixed, a second run silently destroys the first run's report — including the report from a `--move` run that cannot be repeated.
* **Recommended fix:** Stream duplicates to the file as they are found (folds into F5b), and either timestamp the filename or append with a run header.
* **Tradeoffs / Risks:** Timestamped filenames accumulate; a fixed name with an appended header keeps things tidy. Either is acceptable — pick one and document it.
* **Expected impact estimate:** Reliability only.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### F11 — No dry-run for an irreversible operation

* **Category:** Reliability
* **Severity:** Low
* **Impact:** User safety; wasted full runs
* **Evidence:** `organizer.py:140-143` gates `--move` behind a y/N prompt, but there is no way to *preview* what the move would do. `parse_arguments` (`organizer.py:146-154`) exposes no `--dry-run`.
* **Why it's inefficient:** The only way to learn what the tool will do is to let it do it. Users who want to check first must run a full copy (paying the entire I/O cost) and then delete the result. A `--dry-run` that walks, categorizes, and reports without writing costs a fraction of that — and with F1 adopted, it costs almost nothing, since unique-size files are never even read.
* **Recommended fix:** Add `--dry-run` that follows the identical decision path but replaces the `shutil` calls with a log line.
* **Tradeoffs / Risks:** The dry run cannot perfectly predict collision suffixes, because those depend on files created earlier in the same run — simulate them against the in-memory counter map from F2 rather than the filesystem.
* **Expected impact estimate:** Avoids entire wasted runs; qualitative.
* **Removal Safety:** Safe
* **Reuse Scope:** local file

---

### Code Reuse & Dead Code — clean, with two trivial notes

I checked every module-level name for reachability and every function for duplicated logic. **There is no dead code and no meaningful duplication in this file.** Specifically verified:

* `FILE_CATEGORIES`, `UNMATCHED_CATEGORY`, `HASH_CHUNK_SIZE`, `EXTENSION_TO_CATEGORY` — all read at runtime.
* Every function (`get_unique_target_path`, `resolve_target_directory`, `compute_file_hash`, `collect_media_files`, `prompt_organize_mode`, `confirm_move_operation`, `parse_arguments`, `main`) is reachable from `main()`.
* No unreachable branches, no always-true/false conditions, no deprecated paths.
* `EXTENSION_TO_CATEGORY` is built once at import (`organizer.py:27-29`) rather than per file — already the right call.
* **Verified no extension appears in two categories** (95 extensions across 11 categories → 95 map entries, zero collisions). A collision would have been silently resolved by dict-comprehension ordering, so this is worth an assertion or a unit test to keep it true as categories are added.

Two minor notes, neither urgent:

* **Reuse Opportunity (trivial):** `output_dir.mkdir(parents=True, exist_ok=True)` appears at both `organizer.py:108` and `organizer.py:124`. Patch D subsumes the first; the second becomes redundant once any file has been written.
* **Over-Abstracted Code (borderline, leave as is):** `resolve_target_directory` (`organizer.py:45-46`) is a one-line function with a single call site. It names a real concept and costs nothing meaningful — the inlining would be a wash. Not worth changing.

---

## 3) Quick Wins (Do First)

Ordered by impact-per-minute. All four are local to `collect_media_files` and its helpers, and together they address the entire measured hot path.

| Order | Change | Finding | Effort | Measured / expected gain | Risk |
|---|---|---|---|---|---|
| 1 | Replace `output_dir in file_path.resolve().parents` with `file_path.is_relative_to(output_dir)`, and move the `is_symlink()` check *above* it | F3 | ~2 min | **56% faster traversal** | none |
| 2 | Memoize the collision counter in `get_unique_target_path` | F2 | ~10 min | **58×-212×** on collision-heavy dirs | none |
| 3 | Cache created target directories in a `set` | F4 | ~5 min | ~12 `mkdir` calls instead of one per file | none |
| 4 | Size-bucket before hashing (lazy hash) + store the final path in the index | F1, F8 | ~45 min | **5.4× dedup, −82% bytes read**; fixes the stale-path report bug | low |

Items 1-3 are mechanical and independently safe. Item 4 is the largest win and carries the one real subtlety in this report (the post-move re-read); do it last, with the parity test from §5 in place.

---

## 4) Deeper Optimizations (Do Next)

* **Pruned `os.scandir` walk** (F3, patch C). Supersedes quick win #1 and delivers the full 27×. Held back from the quick-win list only because symlinked-directory semantics need deliberate verification. Best done together with F1, since the walk hands the file size to the size-bucketing for free — one syscall serving both.
* **Streamed duplicate report + raw digests** (F5, F10). Removes the two unbounded in-memory structures and makes partial runs recoverable.
* **`--dry-run`** (F11). Cheap once F1 lands, and it is the natural safety net for `--move`.
* **Fused hash+copy pass** (F6). Only for the hash-required branch, only after measuring. Genuinely might not help.
* **Parallel copy stage** (F7). Highest risk, most conditional payoff. Default must stay serial; expose `--jobs`. Do not start this until a profile of the post-F1 code still shows I/O wait dominating.
* **Persistent hash cache across runs** (architectural). Key on `(path, size, mtime_ns)` in a small SQLite or JSON sidecar so repeat runs skip re-hashing unchanged files entirely. This is the natural endpoint if the tool is run regularly against a growing library, and it composes cleanly with F1 (the cache is consulted only when the size bucket is contested). Only worth building if repeat runs over a mostly-stable corpus are a real usage pattern — confirm before investing.

---

## 5) Validation Plan

**Benchmarks.** Build a fixed corpus generator (size distribution, duplicate ratio, and filename-collision ratio as parameters) so runs are comparable across changes. Three profiles, since the findings respond to different pressures:

| Profile | Files | Purpose | Finding under test |
|---|---|---|---|
| `photo-dump` | 20k unique-size images, few collisions | baseline realistic case | F1 |
| `flat-collide` | 5k files, 50 distinct basenames, `--mode flat` | forces suffix collisions | F2 |
| `rerun` | run twice against the same tree | output-subtree rescan | F3 |

Time with `hyperfine --warmup 1 --runs 5` for stable wall-clock deltas. Run each profile on **both** warm and cold page cache (`echo 3 > /proc/sys/vm/drop_caches` on the host — not available inside this container, which is why the numbers above are warm-cache and therefore *conservative* for the I/O findings).

**Profiling.** `python -X importtime` to rule out startup noise, then `cProfile` for call-count attribution and `py-spy record` for a wall-clock flame graph (it captures time blocked in syscalls, which `cProfile` under-attributes). Syscall volume is the crispest evidence for F2/F3/F4 — count it directly:

```bash
strace -f -c -e trace=stat,statx,newfstatat,mkdir,openat python organizer.py --source corpus --output out --mode flat
```

Expect `statx`/`newfstatat` to fall sharply after F2/F3 and `mkdir` to collapse to ~12 after F4.

**Metrics to compare before/after.** Instrument the script with counters and log them at the end — they make regressions visible without re-profiling:

* wall-clock total, and per-stage (walk / hash / copy)
* **files hashed** and **bytes hashed** (the direct F1 metric — target: −80% on `photo-dump`)
* stat syscalls (F2/F3), mkdir syscalls (F4)
* peak RSS via `resource.getrusage(RUSAGE_SELF).ru_maxrss` (F5)
* files processed, duplicates skipped, errors — must be **identical** before and after

**Correctness tests** (`pytest` 9.0.3 is installed). These are the gate on every change above:

1. **Golden-tree parity** — run the current implementation and the optimized one against an identical corpus; assert the resulting output trees are byte-identical (compare sorted `(relative_path, sha256)` tuples) and that `processed/duplicate/error` counts match exactly. This is the single most valuable test: it makes the F1 rewrite verifiable rather than argued.
2. **Duplicate detection parity** — same-content/different-name, same-name/different-content, same-size/different-content (the case size-bucketing must not get wrong — construct files of identical size with differing bytes), and zero-byte files (all share size 0).
3. **`--move` + duplicate** — assert the report's cited original path **exists** after the run (regression test for F8, which currently fails).
4. **Collision suffixes** — N same-named distinct files produce exactly `name`, `name_1` … `name_{N-1}`, with no gaps and no overwrites.
5. **Symlinks** — a file symlink is skipped with a warning; a *directory* symlink is not descended (guards the F3 rewrite against changing `recurse_symlinks=False` semantics); a symlink loop terminates.
6. **Output containment** — output inside source is never re-ingested, including on a second run; `output == source` behaves per F9.
7. **Extension-map integrity** — assert `sum(len(v) for v in FILE_CATEGORIES.values()) == len(EXTENSION_TO_CATEGORY)` to keep the verified no-collision property true as categories grow.
8. **Error paths** — unreadable file (chmod 000) increments `error_count` and does not abort the run.

Run `ruff check` as part of the gate; it currently passes clean.

---

## 6) Optimized Code / Patch (proposals — not applied)

These are proposals only. Nothing in this report has been applied to the working tree.

### Patch A — Size-bucketed lazy hashing (F1 + F8)

Replaces the `seen_hashes` index. The key idea: a file is hashed only once another file with the **same size** appears. `_Entry` tracks the file's *final* path so the deferred hash still works after a `--move`, which simultaneously fixes F8.

```python
from dataclasses import dataclass

@dataclass
class _Entry:
    """A processed file, tracked by its FINAL location so it stays readable after --move."""
    final_path: Path
    digest: bytes | None = None   # computed lazily, only on size contention


def _digest_of(entry: _Entry) -> bytes:
    if entry.digest is None:
        entry.digest = compute_file_digest(entry.final_path)
    return entry.digest


# in collect_media_files, replacing seen_hashes:
size_index: dict[int, list[_Entry]] = {}

# ... per file, with `size` supplied free by the walk (patch C):
bucket = size_index.setdefault(size, [])

if bucket:                                  # size contention -> hashing is now justified
    try:
        incoming = compute_file_digest(file_path)
    except OSError as error:
        error_count += 1
        logger.error("Failed to read '%s': %s", file_path, error)
        continue
    match = next((e for e in bucket if _digest_of(e) == incoming), None)
    if match is not None:
        duplicate_count += 1
        write_duplicate(f"{file_path}  (duplicate of {match.final_path})")
        logger.info("Duplicate skipped: %s", file_path)
        continue
else:
    incoming = None                         # unique size so far -> never read the file

target_dir = ensure_target_dir(output_dir, category, mode, created_dirs)
target_path = get_unique_target_path(target_dir / file_path.name, counters)
# ... perform the copy/move ...
bucket.append(_Entry(final_path=target_path, digest=incoming))
```

**What changed:** the unconditional `compute_file_hash` at `organizer.py:94` is gone. Files with a unique size are never opened. When a size collides, both the newcomer and any not-yet-hashed prior occupants of that bucket are hashed on demand — so no duplicate is ever missed, since equal content implies equal size. The index now stores `target_path` rather than the source path, which is what makes the deferred hash readable post-move (and fixes the report bug F8).

Pair it with a `digest`-returning variant to halve the key memory (F5a):

```python
def compute_file_digest(file_path: Path) -> bytes:
    """SHA-256 as raw bytes (32B, vs 64B for hexdigest); chunked to bound memory."""
    hasher = hashlib.sha256()
    with file_path.open("rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.digest()
```

### Patch B — Memoized collision counter (F2)

```python
def get_unique_target_path(target_path: Path, counters: dict[tuple, int]) -> Path:
    """Non-colliding path. Resumes from the last used suffix instead of rescanning from 1."""
    key = (target_path.parent, target_path.stem, target_path.suffix)
    counter = counters.get(key, 0)

    candidate = (target_path if counter == 0
                 else target_path.with_name(f"{target_path.stem}_{counter}{target_path.suffix}"))
    while candidate.exists():                     # backstop: pre-existing / externally created files
        counter += 1
        candidate = target_path.with_name(f"{target_path.stem}_{counter}{target_path.suffix}")

    counters[key] = counter + 1
    return candidate
```

**What changed:** `counter` resumes from the memoized value instead of restarting at 1, turning n(n+1)/2 stat calls into ~n. The `while exists()` loop is deliberately **kept** so behavior is unchanged when the destination already contains files or is being written by another process — the memo is an optimization hint, not the source of truth.

### Patch C — Pruned `scandir` walk yielding size for free (F3)

```python
def iter_source_files(source_dir: Path, output_dir: Path) -> Iterator[tuple[Path, int]]:
    """Yield (path, size) for regular files under source_dir, pruning the output subtree.

    Symlinks are never followed - matching Path.rglob(recurse_symlinks=False).
    """
    output_str = str(output_dir)
    stack = [str(source_dir)]

    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        logger.warning("Skipped symlink (not followed): %s", entry.path)
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if entry.path != output_str:      # prune: never descend into output
                            stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        yield Path(entry.path), entry.stat(follow_symlinks=False).st_size
        except OSError as error:
            logger.error("Failed to scan '%s': %s", current, error)
```

**What changed:** three wins from one rewrite. The output subtree is skipped at *descent* time rather than filtered after enumeration (the 27×); the per-file `resolve()` is gone entirely (the 56%); and `entry.stat().st_size` comes from the same stat that `is_file()` already needed — handing Patch A its size discriminator at zero additional syscall cost. The symlink warning fires before any expensive check, correcting the guard ordering at `organizer.py:81-85`. Unreadable directories are now logged instead of aborting the walk.

### Patch D — Memoized directory creation (F4)

```python
def ensure_target_dir(output_dir: Path, category: str, mode: str, created: set[Path]) -> Path:
    target_dir = output_dir / category if mode == "folder" else output_dir
    if target_dir not in created:
        target_dir.mkdir(parents=True, exist_ok=True)
        created.add(target_dir)
    return target_dir
```

**What changed:** absorbs `resolve_target_directory` and drops the per-file `mkdir` at `organizer.py:108` to at most one call per distinct target directory (≤12).

### Patch E — Reject the degenerate output configuration (F9)

```python
# in main(), after resolving source_dir and output_dir:
if output_dir == source_dir:
    logger.error(
        "--output must differ from --source (every file would be treated as existing output). "
        "Try --output %s", source_dir / "OrganizedMedia",
    )
    return
```

**What changed:** the silent 0-file no-op verified in F9 becomes an actionable error, before the full tree walk is paid for.

### Also worth folding in (F5b / F10)

Replace the accumulate-then-`write_text` at `organizer.py:123-127` with a lazily-opened append handle, so the report survives an interrupted run and never holds the full list in memory:

```python
report_handle = None
def write_duplicate(line: str) -> None:
    nonlocal report_handle
    if report_handle is None:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_handle = (output_dir / "duplicates_report.txt").open("a", encoding="utf-8")
        report_handle.write(f"# run {datetime.now().isoformat(timespec='seconds')}\n")
    report_handle.write(line + "\n")
```

Opening lazily preserves the current behavior of not creating a report file when there are no duplicates; appending with a run header preserves prior runs' records instead of clobbering them.

---

## Assumptions & Confidence

* **Measured, high confidence:** F1 (5.4×, −82% bytes), F2 (58×/212×, quadratic growth confirmed by the 4×-files → 15.6×-time scaling), F3 (27× traversal, 56% from `resolve()` alone), F5 (~405 B/entry), F9 (reproduced: 0 files processed), and the no-collision extension-map property (95/95).
* **Reasoned from code, not measured:** F4, F6, F8, F10, F11. F8 in particular is a straightforward read of `organizer.py:105` against `:113` and is worth confirming with test 3 in §5.
* **Explicitly "likely" — do not act without measuring:** F6 (fused pass may lose to kernel-accelerated copy) and F7 (parallelism can be negative on mechanical disks).
* **Benchmark caveat:** all timings are **warm page cache** on container-local storage; host cache-dropping was unavailable here. Cold-cache and network-filesystem runs should show *larger* gains for F1/F3/F4, since the eliminated work is disk-bound. Re-run §5's cold-cache profile on the target hardware before quoting these figures as production numbers.
* **Not evaluated:** behavior on Windows/macOS (path semantics, `shutil` fast paths differ), cross-filesystem `shutil.move` fallback cost, and any concurrent modification of the source tree during a run.
