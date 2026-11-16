#!/usr/bin/env python3
"""Rainfall totals — per-gauge daily sums, then one combined report."""
import argparse
import os

from Pegasus.api import *

HERE = os.path.dirname(os.path.abspath(__file__))
GAUGES = ["gauge_01", "gauge_02", "gauge_03"]


class RainfallWorkflow:
    def __init__(self, execution_site="condorpool"):
        self.site = execution_site
        self.wf = Workflow("rainfall_totals")
        self.rc = ReplicaCatalog()
        self.tc = TransformationCatalog()
        self.props = Properties()
        self.props["pegasus.transfer.threads"] = "16"

    def catalogs(self):
        # Raw gauge readings.
        for g in GAUGES:
            self.rc.add_replica("local", f"{g}.csv", os.path.join(HERE, "data", f"{g}.csv"))

        self.rc.add_replica("local", "summarise.py", os.path.join(HERE, "bin", "summarise.py"))

        container = Container(
            "rainfall_env",
            Container.SINGULARITY,
            image="file://" + os.path.join(HERE, "Apptainer", "rainfall.sif"),
            image_site="local",
        )
        self.tc.add_containers(container)

        self.tc.add_transformations(
            Transformation("summarise", site=self.site,
                           pfn=os.path.join(HERE, "bin", "summarise.py"),
                           is_stageable=True),
            Transformation("combine", site=self.site,
                           pfn=os.path.join(HERE, "bin", "combine.py"),
                           is_stageable=True),
        )

    def jobs(self):
        summaries = []
        for g in GAUGES:
            out = File(f"{g}.tab")
            summaries.append(out)
            self.wf.add_jobs(
                Job("summarise")
                .add_args("--gauge", g, "--out", f"results/{g}.tab")
                .add_inputs(File(f"{g}.csv"), File("station-metadata.csv"))
                .add_outputs(out, stage_out=False)
            )

        report = File("rainfall-report.csv")
        self.wf.add_jobs(
            Job("combine")
            .add_args("--out", "rainfall-report.csv")
            .add_inputs(*summaries)
            .add_outputs(report, stage_out=False)
        )

    def write(self, path):
        self.catalogs()
        self.jobs()
        self.wf.add_replica_catalog(self.rc)
        self.wf.add_transformation_catalog(self.tc)
        self.wf.write(path)
        print(f"wrote {path}")


def main():
    ap = argparse.ArgumentParser(description="Generate the rainfall-totals workflow")
    ap.add_argument("-o", "--output", default="workflow.yml")
    ap.add_argument("-e", "--execution-site-name", default="condorpool")
    args = ap.parse_args()
    RainfallWorkflow(args.execution_site_name).write(args.output)


if __name__ == "__main__":
    main()
