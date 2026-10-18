# docling-pipelines-extras

## Build and upload

Requires the sibling checkout `../docling-pipelines`. Enter your Docker Hub token at the login prompt.

```bash
docker login -u kevin1kurian

# CPU image
docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=cpu -f batching/Dockerfile -t docling-pipelines:v2.0.1-cpu ../docling-pipelines
docker tag docling-pipelines:v2.0.1-cpu kevin1kurian/docling-pipelines:v2.0.1-cpu
docker push kevin1kurian/docling-pipelines:v2.0.1-cpu

# GPU image
docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=gpu -f batching/Dockerfile -t docling-pipelines:v2.0.1-gpu ../docling-pipelines
docker tag docling-pipelines:v2.0.1-gpu kevin1kurian/docling-pipelines:v2.0.1-gpu
docker push kevin1kurian/docling-pipelines:v2.0.1-gpu
```

Run from this repository root in Bash. Compose reads the existing `.env`.
For each case, start the stack, run the manual test below, then stop it before selecting the next case.

## CPU without limits

```bash
CASE=cpu-unlimited
COMPOSE=(docker compose --env-file .env -f batching/docker-compose-cpu.yml -f batching/docker-compose-infra.yml)
"${COMPOSE[@]}" up -d --wait
```

## CPU with limits

```bash
CASE=cpu-limited
COMPOSE=(docker compose --env-file .env -f batching/with_limits/docker-compose-cpu.yml -f batching/with_limits/docker-compose-infra.yml)
"${COMPOSE[@]}" up -d --wait
```

## GPU without limits

```bash
CASE=gpu-unlimited
COMPOSE=(docker compose --env-file .env -f batching/docker-compose-gpu.yml -f batching/docker-compose-infra.yml)
"${COMPOSE[@]}" up -d --wait
```

## GPU with limits

```bash
CASE=gpu-limited
COMPOSE=(docker compose --env-file .env -f batching/with_limits/docker-compose-gpu.yml -f batching/with_limits/docker-compose-infra.yml)
"${COMPOSE[@]}" up -d --wait
```

## Manual test — same commands for every running case

```bash
"${COMPOSE[@]}" exec -T ollama ollama pull nomic-embed-text
FLOW_ID=$(curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/flows \
  -H 'Content-Type: application/json' --data-binary @batching/sample_flow.json \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["flow_id"])')
JOB_RUN_ID=$(curl --fail-with-body -sS -X POST http://localhost:8080/api/v1/job_runs \
  -H 'Content-Type: application/json' \
  -d "{\"entity\":{\"job\":{\"asset_ref\":\"$FLOW_ID\"}}}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_run_id"])')

while :; do
  curl --fail-with-body -sS --max-time 30 \
    "http://localhost:8080/api/v1/job_runs/$JOB_RUN_ID" -o "$CASE-job.json"
  STATUS=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["job_stats"]["status"])' "$CASE-job.json")
  case "$STATUS" in
    Completed|CompletedWithErrors|CompletedWithWarnings|Failed|Canceled|Aborted) break ;;
  esac
  sleep 1
done

python3 -c 'import json,sys; s=json.load(open(sys.argv[1]))["job_stats"]; print(s["status"], s["duration"], "seconds")' "$CASE-job.json"
case "$STATUS" in
  Completed|CompletedWithErrors|CompletedWithWarnings)
    curl --fail-with-body -sS --max-time 30 --retry 12 --retry-delay 5 --retry-all-errors \
      "http://localhost:8080/api/v1/job_runs/$JOB_RUN_ID/report" -o "$CASE-report.csv"
    ;;
esac
```

Compare `job_stats.duration` and per-node `time_taken` in `<case>-job.json` (seconds).
`<case>-report.csv` contains per-document processing times.

## Stop before starting the next case

```bash
"${COMPOSE[@]}" down
```
