#!/bin/bash
set -e
pegasus_lite_version_major="5"
pegasus_lite_version_minor="1"
pegasus_lite_version_patch="2"
pegasus_lite_enforce_strict_wp_check="false"
pegasus_lite_version_allow_wp_auto_download="false"
pegasus_metrics="true"


. pegasus-lite-common.sh

pegasus_lite_init

# cleanup in case of failures
trap pegasus_lite_signal_int INT
trap pegasus_lite_signal_term TERM
trap pegasus_lite_unexpected_exit EXIT

printf "\n########################[Pegasus Lite] Setting up workdir ########################\n"  1>&2
# work dir
export pegasus_lite_work_dir=$PWD
pegasus_lite_setup_work_dir

printf "\n##############[Pegasus Lite] Figuring out the worker package to use ##############\n"  1>&2
# figure out the worker package to use
pegasus_lite_worker_package

printf "\n######[Pegasus Lite] Checking file integrity for transferred container files ######\n"  1>&2
# do file integrity checks
pegasus-integrity --print-timings --verify=stdin 1>&2 << 'eof'
nextgen_container.sif
eof
set -e
pegasus_lite_section_start stage_in
printf "\n##################### Setting the xbit for executables staged #####################\n"  1>&2
# set the xbit for any executables staged
/bin/chmod +x outputs_analysis

printf "\n##################### Checking file integrity for input files #####################\n"  1>&2
# do file integrity checks
pegasus-integrity --print-timings --verify=stdin 1>&2 << 'eof'
rundir_gage-10109001_run.tar;;;ngen_outputs_utils.py;;;outputs_analysis;;;ngiab_pegasus.py;;;forcings_utils.py;;;gage-10109001_usgs_obs.csv;;;cal_utils.py;;;nextgen_container.sif;;;ngiab_utils.py
eof

pegasus_lite_section_end stage_in

printf "\n########[Pegasus Lite] Writing out script to launch user task in container ########\n"  1>&2

cat <<EOF > outputs_analysis_analysis_gage_10109001-cont.sh
#!/bin/bash
set -e

# tmp dirs are handled by Singularity - don't use the ones from the host
unset TEMP
unset TMP
unset TMPDIR

# setting environment variables for job
HOME=/srv
export HOME

EOF
container_env /srv >> outputs_analysis_analysis_gage_10109001-cont.sh
cat <<EOF2 >> outputs_analysis_analysis_gage_10109001-cont.sh
pegasus_lite_version_major=$pegasus_lite_version_major
pegasus_lite_version_minor=$pegasus_lite_version_minor
pegasus_lite_version_patch=$pegasus_lite_version_patch
pegasus_lite_enforce_strict_wp_check=$pegasus_lite_enforce_strict_wp_check
pegasus_lite_version_allow_wp_auto_download=$pegasus_lite_version_allow_wp_auto_download
pegasus_lite_inside_container=true
export pegasus_lite_work_dir=/srv

cd /srv
. ./pegasus-lite-common.sh
pegasus_lite_init

printf "\n##############[Container] Figuring out Pegasus worker package to use ##############\n"  1>&2
# figure out the worker package to use
pegasus_lite_worker_package
printf "PATH in container is set to is set to \$PATH\n"  1>&2

printf "\n#########################[Container] Launching user task #########################\n"  1>&2

pegasus-kickstart  -n outputs_analysis -N analysis_gage_10109001 -R condorpool  -s plots/gage-10109001_water_balance.png=plots/gage-10109001_water_balance.png -s analysis/gage-10109001_metrics.csv=analysis/gage-10109001_metrics.csv -s analysis/gage-10109001_basin_means.csv=analysis/gage-10109001_basin_means.csv -L nextgen -T 2026-08-31T15:24:08+00:00 ./outputs_analysis --gage gage-10109001 --run-tar rundir_gage-10109001_run.tar --obs gage-10109001_usgs_obs.csv --output-basin-means analysis/gage-10109001_basin_means.csv --output-metrics analysis/gage-10109001_metrics.csv --output-plot plots/gage-10109001_water_balance.png
EOF2


chmod +x outputs_analysis_analysis_gage_10109001-cont.sh
if ! [ $pegasus_lite_start_dir -ef . ]; then
	cp $pegasus_lite_start_dir/pegasus-lite-common.sh . 
fi

set +e
job_ec=0
singularity_init nextgen_container.sif
job_ec=$(($job_ec + $?))

$singularity_exec exec --no-home --bind $PWD:/srv nextgen_container.sif /srv/outputs_analysis_analysis_gage_10109001-cont.sh
job_ec=$(($job_ec + $?))

pegasus_lite_section_start stage_out
pegasus_lite_section_end stage_out

set -e


# clear the trap, and exit cleanly
trap - EXIT
pegasus_lite_final_exit

