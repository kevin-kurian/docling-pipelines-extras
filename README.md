# docling-pipelines-extras

## Setup

From this repository root:

```bash
uv venv --python 3.12 --no-python-downloads .venv
source .venv/bin/activate
```

## Build and upload

Requires `../docling-pipelines`. Use your Docker Hub token at login.

```bash
docker login -u kevin1kurian

docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=cpu -f batching/Dockerfile -t docling-pipelines:v2.0.1-cpu ../docling-pipelines
docker tag docling-pipelines:v2.0.1-cpu kevin1kurian/docling-pipelines:v2.0.1-cpu
docker push kevin1kurian/docling-pipelines:v2.0.1-cpu

docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=gpu -f batching/Dockerfile -t docling-pipelines:v2.0.1-gpu ../docling-pipelines
docker tag docling-pipelines:v2.0.1-gpu kevin1kurian/docling-pipelines:v2.0.1-gpu
docker push kevin1kurian/docling-pipelines:v2.0.1-gpu
```

## Run the batching tests

Use Bash from this repository root, in the same terminal session.

- PDF: 10 files, 96 pages — `batching/flows/sample_flow_pdf.json`
- Text: 100 files — `batching/flows/sample_flow_text.json`
- GPU cases require an NVIDIA GPU available to Docker.

### Start and stop each case

Start one stack, run and collect PDF and text results, then stop it.

**CPU, unlimited**

```bash
export CASE=cpu-unlimited
docker compose --env-file .env -f batching/docker-compose-cpu.yml -f batching/docker-compose-infra.yml up -d --wait
```

```bash
docker compose --env-file .env -f batching/docker-compose-cpu.yml -f batching/docker-compose-infra.yml down
```

**CPU, limited**

```bash
export CASE=cpu-limited
docker compose --env-file .env -f batching/with_limits/docker-compose-cpu.yml -f batching/with_limits/docker-compose-infra.yml up -d --wait
```

```bash
docker compose --env-file .env -f batching/with_limits/docker-compose-cpu.yml -f batching/with_limits/docker-compose-infra.yml down
```

**GPU, unlimited**

```bash
export CASE=gpu-unlimited
docker compose --env-file .env -f batching/docker-compose-gpu.yml -f batching/docker-compose-infra.yml up -d --wait
```

```bash
docker compose --env-file .env -f batching/docker-compose-gpu.yml -f batching/docker-compose-infra.yml down
```

**GPU, limited**

```bash
export CASE=gpu-limited
docker compose --env-file .env -f batching/with_limits/docker-compose-gpu.yml -f batching/with_limits/docker-compose-infra.yml up -d --wait
```

```bash
docker compose --env-file .env -f batching/with_limits/docker-compose-gpu.yml -f batching/with_limits/docker-compose-infra.yml down
```

### Common test steps

Pull the embedding model:

```bash
curl --fail-with-body -sS -X POST http://localhost:11434/api/pull \
  -H 'Content-Type: application/json' \
  -d '{"name":"nomic-embed-text"}'
```

Submit one flow, run it, then repeat for the other input:

```bash
# Submit the PDF flow
export INPUT_TYPE=pdf
FLOW_NAME="sample_flow_pdf_${CASE}_$(date +%s)"
FLOW_ID=$(sed "s|\"flow_name\": \"sample_flow_pdf\"|\"flow_name\": \"$FLOW_NAME\"|" batching/flows/sample_flow_pdf.json \
  | curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/flows \
      -H 'Content-Type: application/json' --data-binary @- \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["flow_id"])')
```

```bash
# Submit the text flow
export INPUT_TYPE=text
FLOW_NAME="sample_flow_text_${CASE}_$(date +%s)"
FLOW_ID=$(sed "s|\"flow_name\": \"sample_flow_text\"|\"flow_name\": \"$FLOW_NAME\"|" batching/flows/sample_flow_text.json \
  | curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/flows \
      -H 'Content-Type: application/json' --data-binary @- \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["flow_id"])')
```

Run, poll, and collect before stopping the stack:

```bash
JOB_RUN_ID=$(curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/job_runs \
  -H 'Content-Type: application/json' \
  -d "{\"entity\":{\"job\":{\"asset_ref\":\"$FLOW_ID\"}}}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_run_id"])')

while :; do
  curl --fail-with-body -sS --max-time 30 \
    "http://localhost:8080/api/v1/job_runs/$JOB_RUN_ID" -o /tmp/docling-pipelines-job.json
  STATUS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["job_stats"]["status"])' /tmp/docling-pipelines-job.json)
  case "$STATUS" in
    Completed|CompletedWithErrors|CompletedWithWarnings|Failed|Canceled|Cancelled|Aborted) break ;;
  esac
  sleep 1
done

python3 batching/scripts/collect_results.py \
  --job-run-id "$JOB_RUN_ID" --flow-name "$FLOW_NAME" \
  --case "$CASE" --input-type "$INPUT_TYPE"
rm -f /tmp/docling-pipelines-job.json
```

### Results

Generated files:

```text
batching/results/
├── results.csv
└── <flow_name>_<timestamp>/
    ├── flow_run.log
    ├── node_execution.log
    ├── job_stats.json
    └── summary.csv
```

## Run tests with CPU and memory collection

Example: run the PDF flow on CPU without container resource limits. Start the
CPU-unlimited stack and pull the embedding model using the commands above, then
run from this repository root:

```bash
source .venv/bin/activate
uv pip install -r requirements.txt
python batching/scripts/run_test.py \
  --case cpu-unlimited \
  --env-file .env \
  --compose-file batching/docker-compose-cpu.yml \
  --compose-file batching/docker-compose-infra.yml \
  --flow-file batching/flows/sample_flow_pdf.json \
  --colima-profile default \
  --interval 1 --before-seconds 30 --after-seconds 30
```

Change the flow file, Compose files, case label, Colima profile, and timing
parameters to match your test.

- `--flow-file`: one or more flow JSON files; repeatable and accepts shell globs.
- `--compose-file`: repeat for each Compose file; Docker merges them in the order
  supplied. This example combines the CPU application and shared infrastructure files.
  `--case` is a results label; it does not select the stack.
- `--service`: restrict monitoring to selected services; repeatable. Default: all containers in the stack.
- `--colima-profile`: repeat for multiple VMs. Use `--skip-colima` on non-Colima hosts.
- `--api-url`, `--job-config`, `--project-name`, `--results-dir`: configure the API,
  optional job submission JSON, Compose project, and output directory.
- `--repeat`, `--interval`, `--before-seconds`, `--after-seconds`, `--timeout-seconds`:
  configure repetitions, sampling windows, and the maximum job run time.

Additional generated files in each run folder:

```text
resource_samples.csv
resource_samples.png
resource_summary.csv
run_metadata.json
```

Run `python batching/scripts/run_test.py --help` for all parameters.
