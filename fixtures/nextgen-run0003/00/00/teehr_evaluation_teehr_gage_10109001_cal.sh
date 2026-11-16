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
teehr_container.sif
eof
set -e
pegasus_lite_section_start stage_in
printf "\n##################### Setting the xbit for executables staged #####################\n"  1>&2
# set the xbit for any executables staged
/bin/chmod +x teehr_evaluation

printf "\n##################### Checking file integrity for input files #####################\n"  1>&2
# do file integrity checks
pegasus-integrity --print-timings --verify=stdin 1>&2 << 'eof'
ngen_outputs_utils.py;;;ngiab_pegasus.py;;;teehr_evaluation;;;forcings_utils.py;;;gage-10109001_usgs_obs.csv;;;cal_utils.py;;;rundir_gage-10109001_cal.tar;;;teehr_container.sif;;;ngiab_utils.py
eof

pegasus_lite_section_end stage_in

printf "\n########[Pegasus Lite] Writing out script to launch user task in container ########\n"  1>&2

cat <<EOF > teehr_evaluation_teehr_gage_10109001_cal-cont.sh
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
container_env /srv >> teehr_evaluation_teehr_gage_10109001_cal-cont.sh
cat <<EOF2 >> teehr_evaluation_teehr_gage_10109001_cal-cont.sh
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

pegasus-kickstart  -n teehr_evaluation -N teehr_gage_10109001_cal -R condorpool  -s analysis/gage-10109001_teehr_metrics_cal.csv=analysis/gage-10109001_teehr_metrics_cal.csv -s plots/gage-10109001_teehr_cal.png=plots/gage-10109001_teehr_cal.png -L nextgen -T 2026-08-31T15:24:08+00:00 ./teehr_evaluation --gage gage-10109001 --run-tar rundir_gage-10109001_cal.tar --obs gage-10109001_usgs_obs.csv --start 2017-10-01 --end 2021-09-30 --training-start 2019-10-01 --output-metrics analysis/gage-10109001_teehr_metrics_cal.csv --output-plot plots/gage-10109001_teehr_cal.png
EOF2


chmod +x teehr_evaluation_teehr_gage_10109001_cal-cont.sh
if ! [ $pegasus_lite_start_dir -ef . ]; then
	cp $pegasus_lite_start_dir/pegasus-lite-common.sh . 
fi

set +e
job_ec=0
singularity_init teehr_container.sif
job_ec=$(($job_ec + $?))

$singularity_exec exec --no-home --bind $PWD:/srv teehr_container.sif /srv/teehr_evaluation_teehr_gage_10109001_cal-cont.sh
job_ec=$(($job_ec + $?))

pegasus_lite_section_start stage_out
pegasus_lite_section_end stage_out

set -e


# clear the trap, and exit cleanly
trap - EXIT
pegasus_lite_final_exit

