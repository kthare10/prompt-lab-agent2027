#!/usr/bin/env python3
"""Combine per-gauge daily totals into one report."""
import argparse
import glob


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = []
    for path in sorted(glob.glob("*.tab")):
        gauge = path.rsplit(".", 1)[0]
        with open(path) as fh:
            next(fh, None)
            for line in fh:
                date, total = line.rstrip("\n").split("\t")
                rows.append((gauge, date, total))

    with open(args.out, "w") as fh:
        fh.write("gauge,date,total_mm\n")
        for gauge, date, total in sorted(rows):
            fh.write(f"{gauge},{date},{total}\n")


if __name__ == "__main__":
    main()
