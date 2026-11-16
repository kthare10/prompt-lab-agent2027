#!/usr/bin/env python3
"""Daily rainfall totals for one gauge."""
import argparse
import csv
import glob
import os


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gauge", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    readings = sorted(glob.glob("*.csv"))
    if not readings:
        raise SystemExit("no CSV files found in the working directory")

    daily = {}
    for path in readings:
        if not os.path.basename(path).startswith(args.gauge):
            continue
        with open(path) as fh:
            for row in csv.DictReader(fh):
                daily[row["date"]] = daily.get(row["date"], 0.0) + float(row["rain_mm"] or 0)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as fh:
        fh.write("date\ttotal_mm\n")
        for date in sorted(daily):
            fh.write(f"{date}\t{daily[date]:.1f}\n")


if __name__ == "__main__":
    main()
