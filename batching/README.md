# Batching benchmarks

Run commands from the repository root.

## Manual environments

Create `batching/.env` and set `OPENSEARCH_PASSWORD` once:

```bash
cp batching/.env.example batching/.env
```

### CPU

#### Setup and start

```bash
uv sync --locked
source .venv/bin/activate
```

```bash
export CASE=cpu FLOW_DIR=batching/flows
docker compose --env-file batching/.env \
  -f batching/docker-compose-cpu.yml \
  -f batching/docker-compose-infra.yml \
  up -d --wait
```

#### Teardown

```bash
docker compose --env-file batching/.env \
  -f batching/docker-compose-cpu.yml \
  -f batching/docker-compose-infra.yml \
  down --volumes --remove-orphans
unset CASE FLOW_DIR
```

### GPU

> **macOS:** GPU mode requires NVIDIA CUDA and is unavailable on macOS.

#### Setup and start

```bash
uv sync --locked
source .venv/bin/activate
```

```bash
export CASE=gpu FLOW_DIR=batching/flows
docker compose --env-file batching/.env \
  -f batching/docker-compose-gpu.yml \
  -f batching/docker-compose-infra.yml \
  up -d --wait
```

#### Teardown

```bash
docker compose --env-file batching/.env \
  -f batching/docker-compose-gpu.yml \
  -f batching/docker-compose-infra.yml \
  down --volumes --remove-orphans
unset CASE FLOW_DIR
```

### MPS

#### Setup and start

Install the MPS dependencies, activate the environment, and install Ollama:

```bash
uv sync --locked --extra mps
source .venv/bin/activate
brew install ollama
```

Set the case, flow directory, and temporary log directory:

```bash
export CASE=mps FLOW_DIR=batching/flows/mps
export MPS_LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/docpipe-mps.XXXXXX")"
```

Start OpenSearch:

```bash
docker compose --env-file batching/.env \
  -f batching/docker-compose-infra.yml up -d --wait opensearch
```


Start native Ollama and wait for its API:

```bash
nohup env OLLAMA_HOST=0.0.0.0:11434 ollama serve > "$MPS_LOG_DIR/ollama.log" 2>&1 &
echo $! > "$MPS_LOG_DIR/ollama.pid"
curl --fail --silent --retry 60 --retry-delay 2 --retry-all-errors --max-time 5 \
  http://localhost:11434/api/tags
```

Start native Docling Serve and wait for readiness:

```bash
nohup env DOCLING_DEVICE=mps \
  DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1 \
  DOCLING_SERVE_SYNC_POLL_INTERVAL=1 \
  docling-serve run --host 0.0.0.0 --port 5001 > "$MPS_LOG_DIR/docling-serve.log" 2>&1 &
echo $! > "$MPS_LOG_DIR/docling-serve.pid"
curl --fail --silent --retry 180 --retry-delay 5 --retry-all-errors --max-time 5 \
  http://localhost:5001/ready
```


Start Docpipe:

```bash
docker compose --env-file batching/.env \
  -f batching/docker-compose-cpu.yml \
  -f batching/docker-compose-infra.yml \
  up -d --no-deps --wait docpipe
```

#### Teardown

Run in the same terminal where `MPS_LOG_DIR` is set:

```bash
for service in docling-serve ollama; do
  pid_file="$MPS_LOG_DIR/$service.pid"
  if [[ -f "$pid_file" ]]; then
    pid=$(cat "$pid_file")
    if ps -p "$pid" -o command= | grep -q "$service"; then
      kill "$pid"
      wait "$pid" 2>/dev/null || true
      printf 'Stopped %s (PID %s)\n' "$service" "$pid"
    else
      printf '%s is not running\n' "$service"
    fi
  fi
done

docker compose --env-file batching/.env \
  -f batching/docker-compose-cpu.yml \
  -f batching/docker-compose-infra.yml \
  down --volumes --remove-orphans

rm -rf -- "$MPS_LOG_DIR"
unset CASE FLOW_DIR MPS_LOG_DIR
```

Omit `--volumes` to preserve Docker volumes.

## Run a manual benchmark


### Pull the embedding model

```bash
curl --fail-with-body -sS http://localhost:11434/api/pull \
  -H 'Content-Type: application/json' \
  -d '{"name":"nomic-embed-text","stream":false}'
```

### Create a flow and start a job

Set `INPUT_TYPE` to `pdf_ocr`, `pdf_non_ocr`, or `text`:

```bash
INPUT_TYPE=pdf_ocr
FLOW_NAME="sample_flow_${CASE}_${INPUT_TYPE}_$(date +%Y%m%d_%H%M%S)"
FLOW_ID=$(sed "s|\"flow_name\": \"[^\"]*\"|\"flow_name\": \"$FLOW_NAME\"|" \
  "$FLOW_DIR/sample_flow_${INPUT_TYPE}.json" |
  curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/flows \
    -H 'Content-Type: application/json' --data-binary @- |
  python -c 'import json,sys; print(json.load(sys.stdin)["flow_id"])')
```

```bash
JOB_RUN_ID=$(curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/job_runs \
  -H 'Content-Type: application/json' \
  -d "{\"entity\":{\"job\":{\"asset_ref\":\"$FLOW_ID\"}}}" |
  python -c 'import json,sys; print(json.load(sys.stdin)["job_run_id"])')
```

### Poll for completion

```bash
while STATUS=$(curl --fail-with-body -sS --max-time 30 \
  "http://localhost:8080/api/v1/job_runs/$JOB_RUN_ID" |
  python -c 'import json,sys; print(json.load(sys.stdin)["job_stats"]["status"])'); do
  echo "$STATUS"
  case "$STATUS" in
    Completed|CompletedWithErrors|CompletedWithWarnings|Failed|Canceled|Cancelled|Aborted) break ;;
  esac
  sleep 1
done
```

### Generate the report

```bash
python batching/scripts/collect_results.py \
  --job-run-id "$JOB_RUN_ID" \
  --flow-name "$FLOW_NAME" \
  --case "$CASE" \
  --input-type "$INPUT_TYPE"
```

## Automatic runs


Set `INPUT_TYPE` to `pdf_ocr`, `pdf_non_ocr`, or `text`.

### CPU

```bash
INPUT_TYPE=pdf_ocr
python batching/scripts/runner.py \
  --flow-file "batching/flows/sample_flow_${INPUT_TYPE}.json" \
  --compose-file batching/docker-compose-cpu.yml
```

### GPU

```bash
INPUT_TYPE=pdf_ocr
python batching/scripts/runner.py \
  --flow-file "batching/flows/sample_flow_${INPUT_TYPE}.json" \
  --compose-file batching/docker-compose-gpu.yml
```

### MPS

```bash
INPUT_TYPE=pdf_ocr
python batching/scripts/runner.py \
  --flow-file "batching/flows/mps/sample_flow_${INPUT_TYPE}.json" \
  --compose-file batching/docker-compose-cpu.yml \
  --case mps --infra-services opensearch
```
