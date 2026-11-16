set -u
cd /opt/prompt-lab
# Provider ids are per-deployment, not properties of the provider: Studio mints
# one per row, so the opaque `custom-mtd4*` ids these runs were measured under
# exist only on the machine that created them. The names are the stable part,
# so they are the knob; the defaults are this deployment's ids.
FABRIC="${FABRIC:-custom-fabric}"
NRP="${NRP:-custom-nrp}"
AZURE="${AZURE:-custom-azure}"

python3 -u -m harness.sweep --run-id s1-fabric --provider $FABRIC --repeats 1 --depth auto --turn-timeout 2400 \
  --tasks t00-hello-pegasus,t01-sensor-summary,t02-tile-reduce,t03-legacy-tool-container,t04-tide-gauge-fetch,t05a-snakemake-tnseq,t05b-snakemake-photometry,t06-wrapper-only,t08-debug-failed,t10-ambiguous-spec,t11-field-trial,t12-federated-rounds,t13-barcode-split,t14-kernel-sweep --models gpt-oss-20b,minimax-m2.7,nemotron-nano-30b,qwen3.5-122b
python3 -u -m harness.sweep --run-id s1-nrp --provider $NRP --repeats 1 --depth auto --turn-timeout 2400 \
  --tasks t00-hello-pegasus,t01-sensor-summary,t02-tile-reduce,t03-legacy-tool-container,t04-tide-gauge-fetch,t05a-snakemake-tnseq,t05b-snakemake-photometry,t06-wrapper-only,t08-debug-failed,t10-ambiguous-spec,t11-field-trial,t12-federated-rounds,t13-barcode-split,t14-kernel-sweep --models deepseek-v4-flash,gemma,gemma-small,gemma-small-e4b,gemma4-12b,gemma4-small,glm-5,gpt-oss,kimi,minimax-m2,qwen3,qwen3-4bit,qwen3-small
python3 -u -m harness.sweep --run-id s1-azure --provider $AZURE --repeats 1 --depth auto --turn-timeout 2400 \
  --tasks t00-hello-pegasus,t01-sensor-summary,t02-tile-reduce,t03-legacy-tool-container,t04-tide-gauge-fetch,t05a-snakemake-tnseq,t05b-snakemake-photometry,t06-wrapper-only,t08-debug-failed,t10-ambiguous-spec,t11-field-trial,t12-federated-rounds,t13-barcode-split,t14-kernel-sweep --models Kimi-K2.6
echo "=== STAGE 1 SWEEP COMPLETE ==="
