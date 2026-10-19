# docling-pipelines-extras

## Setup

### CPU/GPU

```bash
uv sync --locked
source .venv/bin/activate
```

### MPS

The MPS flows use Apple Vision OCR (`ocrmac`) and Metal-accelerated layout detection.

```bash
uv sync --locked --extra mps
source .venv/bin/activate
brew install ollama
```

## Start the environment

### CPU

```bash
export CASE=cpu FLOW_DIR=batching/flows
docker compose --env-file .env -f batching/docker-compose-cpu.yml -f batching/docker-compose-infra.yml up -d --wait
```

### GPU

```bash
export CASE=gpu FLOW_DIR=batching/flows
docker compose --env-file .env -f batching/docker-compose-gpu.yml -f batching/docker-compose-infra.yml up -d --wait
```

### MPS

```bash
export CASE=mps FLOW_DIR=batching/flows/mps
export MPS_LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/docpipe-mps.XXXXXX")"
```

- Start OpenSearch

  ```bash
  docker compose --env-file .env -f batching/docker-compose-infra.yml up -d --wait opensearch
  ```

- Start Docpipe

  ```bash
  docker compose --env-file .env -f batching/docker-compose-cpu.yml -f batching/docker-compose-infra.yml up -d --no-deps --wait docpipe
  ```

- Start Ollama

  ```bash
  nohup env OLLAMA_HOST=0.0.0.0:11434 ollama serve > "$MPS_LOG_DIR/ollama.log" 2>&1 &
  curl --fail -sS --retry 60 --retry-delay 2 --retry-all-errors --max-time 5 http://localhost:11434/api/tags
  ```

- Start Docling Serve

  ```bash
  nohup env DOCLING_DEVICE=mps \
    DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1 \
    DOCLING_SERVE_SYNC_POLL_INTERVAL=1 \
    docling-serve run --host 0.0.0.0 --port 5001 > "$MPS_LOG_DIR/docling-serve.log" 2>&1 &
  curl --fail -sS --retry 180 --retry-delay 5 --retry-all-errors --max-time 5 http://localhost:5001/ready
  ```

## Pull the embedding model

```bash
curl --fail-with-body -sS http://localhost:11434/api/pull -H 'Content-Type: application/json' -d '{"name":"nomic-embed-text","stream":false}'
```

## Create the flow

Set `INPUT_TYPE` to `pdf_ocr`, `pdf_non_ocr`, or `text`.

```bash
INPUT_TYPE=pdf_ocr
FLOW_NAME="sample_flow_${INPUT_TYPE}_${CASE}_$(date +%s)"
FLOW_ID=$(sed "s|\"flow_name\": \"[^\"]*\"|\"flow_name\": \"$FLOW_NAME\"|" "$FLOW_DIR/sample_flow_${INPUT_TYPE}.json" | curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/flows -H 'Content-Type: application/json' --data-binary @- | python -c 'import json,sys; print(json.load(sys.stdin)["flow_id"])')
```

## Start the job run

```bash
JOB_RUN_ID=$(curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/job_runs -H 'Content-Type: application/json' -d "{\"entity\":{\"job\":{\"asset_ref\":\"$FLOW_ID\"}}}" | python -c 'import json,sys; print(json.load(sys.stdin)["job_run_id"])')
```

## Poll for completion

```bash
while STATUS=$(curl --fail-with-body -sS --max-time 30 "http://localhost:8080/api/v1/job_runs/$JOB_RUN_ID" | python -c 'import json,sys; print(json.load(sys.stdin)["job_stats"]["status"])'); do
  echo "$STATUS"
  case "$STATUS" in
    Completed|CompletedWithErrors|CompletedWithWarnings|Failed|Canceled|Cancelled|Aborted) break ;;
  esac
  sleep 1
done
```

## Generate the report

```bash
python batching/scripts/collect_results.py --job-run-id "$JOB_RUN_ID" --flow-name "$FLOW_NAME" --case "$CASE" --input-type "$INPUT_TYPE"
```

## Automatic runs

### CPU

```bash
python batching/scripts/runner.py --flow-file batching/flows/sample_flow_pdf_ocr.json --compose-file batching/docker-compose-cpu.yml
```

### GPU

```bash
python batching/scripts/runner.py --flow-file batching/flows/sample_flow_pdf_ocr.json --compose-file batching/docker-compose-gpu.yml
```

### MPS

```bash
python batching/scripts/runner.py --flow-file batching/flows/mps/sample_flow_pdf_ocr.json --compose-file batching/docker-compose-cpu.yml --case mps --infra-services opensearch
```

### Separate OCR and non-OCR PDF runs

Each subset contains 10 PDFs from [performance-dataset-bo10k](https://huggingface.co/datasets/docling-project/performance-dataset-bo10k). The source has no OCR labels; the selections are documented in [ocr_manifest.json](datasets/10-pdf-files/ocr_manifest.json) and [non_ocr_manifest.json](datasets/10-pdf-files/non_ocr_manifest.json).

- `datasets/10-pdf-files/pdf/ocr/`: 99 pages; OCR recovered text absent from the text layer in every document. Includes mixed text/image PDFs.
- `datasets/10-pdf-files/pdf/non-ocr/`: 102 pages; usable embedded text on every nonblank page, without large raster regions.

Use `sample_flow_pdf_ocr.json` for the OCR subset and `sample_flow_pdf_non_ocr.json` for the non-OCR subset. Both have CPU/GPU and MPS versions. The OCR flows enable OCR (RapidOCR on CPU/GPU, Apple Vision on MPS); the non-OCR flows disable it. Reports use the input types `pdf_ocr` and `pdf_non_ocr`.

Run CPU cases first, then start the native services from the MPS setup before running MPS cases:

```bash
source .venv/bin/activate
python batching/scripts/runner.py --flow-file batching/flows/sample_flow_pdf_ocr.json --compose-file batching/docker-compose-cpu.yml
python batching/scripts/runner.py --flow-file batching/flows/sample_flow_pdf_non_ocr.json --compose-file batching/docker-compose-cpu.yml
# Start the native MPS services described above before these two commands.
python batching/scripts/runner.py --flow-file batching/flows/mps/sample_flow_pdf_ocr.json --compose-file batching/docker-compose-cpu.yml --case mps
python batching/scripts/runner.py --flow-file batching/flows/mps/sample_flow_pdf_non_ocr.json --compose-file batching/docker-compose-cpu.yml --case mps
```
