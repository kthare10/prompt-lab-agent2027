# Ground truth for t07 — six seeded defects in `review-01`

**Kept outside `fixtures/review-01/` on purpose.** `seed_files` copies that
directory into the project, so anything inside it is readable by the model under
test. A ground-truth file seeded alongside the code would be an answer key.

Scoring is recall over these six, plus a count of false positives.

| # | defect | where | why it matters |
|---|---|---|---|
| 1 | `station-metadata.csv` is an input to every `summarise` job but has no replica-catalog entry | `workflow_generator.py`, `catalogs()` | the planner cannot stage a file it does not know about |
| 2 | `summarise.py` discovers its inputs with `glob.glob("*.csv")` instead of taking them as arguments | `bin/summarise.py` | whatever Pegasus happened to stage decides what the job reads |
| 3 | the `Container` is declared and staged but never passed to either `Transformation` | `workflow_generator.py`, `catalogs()` | jobs run on the bare node, without the tools the container holds |
| 4 | `summarise.py` is registered in the **replica** catalog, not the transformation catalog | `workflow_generator.py`, `catalogs()` | Pegasus treats the wrapper as data: staged, never made executable |
| 5 | the wrapper writes `results/<gauge>.tab`; the job declares the output as `<gauge>.tab` | `workflow_generator.py` `jobs()` vs `bin/summarise.py` | the planner waits on a file that is never produced under that name |
| 6 | `rainfall-report.csv` is declared `stage_out=False` | `workflow_generator.py`, `jobs()` | the run succeeds and the user never receives the one file they asked for |

## What is deliberately *not* a defect

The original defect list for t07 included *"Apptainer base image is `ubuntu:22.04`
against this 24.04 host"*. **That was removed on 2026-09-04 because it is no
longer true**, and seeding it would have been seeding a lie:

- the host is Debian 13 trixie (glibc 2.41), not Ubuntu 24.04
- `ubuntu:22.04` (glibc 2.35) builds cleanly here with *and* without
  `--ignore-fakeroot-command`, and runs on the pool producing byte-identical
  output to a trixie container — measured, not assumed
- the old `GLIBC_2.38 ... libfakeroot.so` failure needed Apptainer to fall back
  to the fakeroot *command*; builds now use `--fakeroot` backed by a subuid
  range, which never loads that library

Defect 3 (a declared-but-unattached container) replaces it. It is a documented
pitfall, it is locatable by reading the generator, and unlike the base-image
rule it is actually wrong.

The base image here is `debian:trixie` and the definition installs `curl`/`wget`,
both of which are fine. A model flagging either is a **false positive**.
