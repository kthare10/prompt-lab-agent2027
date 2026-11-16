# t08 fixture — a real failed run

Taken from the FABRIC submit host (`pegasus-submit`) on 2026-09-02, from
`~/nextgen-workflow/ubuntu/pegasus/nextgen/run0003`. A NextGen water-modelling
workflow, run by the owner of this project — not a seeded defect.

Trimmed on the way in: the 17.4 MB Pegasus worker tarball, the DAG PNGs, the
`.dot`, and rotated `stampede.db`/`replicas.db` copies. Everything a debugger
reads is intact — `braindump.yml`, `jobstate.log` and its rotations, the
`monitord` logs, the catalogs, the DAG, and every job's `.out`/`.err`/`.sub`.

## What `pegasus-analyzer` reports

```
Total jobs         :     49 (100.00%)
# jobs succeeded   :     33 (67.35%)
# jobs unsubmitted :     16 (32.65%)

calibrate_calibrate_gage_10109001_t5   last state: POST_SCRIPT_FAILED
                                       site: condorpool   exitcode: 1
```

The job failed twice — `jobstate.log` records `JOB_FAILURE` then
`POST_SCRIPT_FAILURE` at both attempt 31 and attempt 35.

## The cause, and the decoy

The chain, in the order it appears in
`00/00/calibrate_calibrate_gage_10109001_t5.out.001`:

| line | what it says |
|---|---|
| 12615 | `Assertion failed in ... nemesis/netmod/tcp/socksm.c at line 569` |
| 12631 | `internal ABORT - process 5` |
| 12639 | `KeyError: ... netCDF4._netCDF4.Dataset ... troute_output_201710010000.nc` |
| 12693 | `FileNotFoundError: [Errno 2] No such file or directory: ...troute_output_201710010000.nc` |
| 12694 | `calibrate failed: [Errno 2] No such file or directory: ...` |

**The cause is the MPI abort.** `ngen-parallel` was launched under
`mpirun -n 8`; an MPI transport assertion killed it partway through the run, so
`troute_output_201710010000.nc` was never written. The `FileNotFoundError` is
what the job *reports*, and it is the last thing in the log — which makes it the
answer a model gets by tailing rather than diagnosing.

## Why this run and not a seeded defect

t08 originally listed four variants (missing replica-catalog entry, GLIBC on an
old base, LFN mismatch, unstaged transformation). In all four the error message
*is* the answer, so they test log-reading rather than diagnosis. Here the error
message and the cause disagree, which is the distinction the task exists to
measure. `run_flow.classify()` is no help either — it covers generate, build,
plan and submit, not job-execution failures.

The GLIBC variant was dropped outright: verified on 2026-09-02 that an
`ubuntu:22.04` base now builds cleanly on this host under
`--ignore-fakeroot-command`, so that failure no longer occurs. The Studio overlay
and `classify()` both still document it.

## Two traps for the grader, not just the model

- **The stderr is worthless.** `...t5.err.001` is 1.6 KB of Pegasus Lite
  scaffolding ending in `Last command exited with 1`. A model that reads only
  stderr cannot possibly get this right, and should not be credited for guessing.
- **The evidence is ~12,600 lines in**, in a 636 KB stdout file. Reaching it
  requires searching the file, not printing it.
