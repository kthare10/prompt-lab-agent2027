# rainfall-totals

Daily rainfall totals per gauge, combined into one report.

    python3 workflow_generator.py -o workflow.yml

Three gauges under `data/`, one CSV each, plus `station-metadata.csv` with the
site names. `bin/summarise.py` produces a per-gauge table; `bin/combine.py`
merges them into `rainfall-report.csv`.

The software environment is `Apptainer/rainfall.def` — Debian with Python.

Not run yet.
