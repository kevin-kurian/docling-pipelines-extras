# docling-pipelines-extras

## Setup

From this repository root:

```bash
uv venv --python 3.12 --no-python-downloads .venv
source .venv/bin/activate
```

## Build and upload

Clone the `docling-pipelines` source repository into the same parent directory as
`docling-pipelines-extras`, so it is available at `../docling-pipelines` when running
commands from this repository root. The Docker builds below use that checkout as
their build context. Use your Docker Hub token at login.

```bash
docker login -u kevin1kurian

docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=cpu -f batching/Dockerfile -t docling-pipelines:v2.0.1-cpu ../docling-pipelines
docker tag docling-pipelines:v2.0.1-cpu kevin1kurian/docling-pipelines:v2.0.1-cpu
docker push kevin1kurian/docling-pipelines:v2.0.1-cpu

docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=gpu -f batching/Dockerfile -t docling-pipelines:v2.0.1-gpu ../docling-pipelines
docker tag docling-pipelines:v2.0.1-gpu kevin1kurian/docling-pipelines:v2.0.1-gpu
docker push kevin1kurian/docling-pipelines:v2.0.1-gpu
```

## Manual execution

Run the commands below from the `docling-pipelines-extras` directory. Use the same
terminal for all steps so the variables set in earlier commands remain available.

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
# Submit the TXT flow
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

Each run is independent. Read its `REPORT.md` for status, document counts,
operator timings, and resource usage with an embedded graph when collected.
Supporting files stay under `_`.

```text
batching/results/
├── pdf_cpu-unlimited_<timestamp>/
│   ├── REPORT.md
│   └── _/
├── text_cpu-limited_<timestamp>/
│   ├── REPORT.md
│   └── _/
└── _/
```

## Automatic test runner

Run from this repository root with Docker running and `.env` configured:

```bash
source .venv/bin/activate
uv pip install -r requirements.txt
python batching/scripts/runner.py \
  --flow-file batching/flows/sample_flow_pdf.json \
  --compose-file batching/docker-compose-cpu.yml
```

Run `python batching/scripts/runner.py --help` for optional settings.

### Review the results

Each run saves its results under `batching/results/`. The runner prints the path
to `REPORT.md`; start there for an overview. Inside the run's `_` folder, you'll
find the job report (`summary.csv`), complete logs (`flow_run.log`), and resource
data and graphs (`resource_*`).
