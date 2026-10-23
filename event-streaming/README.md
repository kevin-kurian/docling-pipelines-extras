# Event streaming benchmarks

Run commands from the repository root. Requires Apple Silicon macOS, Colima running,
and Ollama installed.

## Setup

```bash
cp -n event-streaming/.env.example event-streaming/.env
uv sync --locked --python 3.12 --extra event-streaming --extra mps
source .venv/bin/activate
```

Set `OPENSEARCH_PASSWORD` in `event-streaming/.env`. GPU collection requires sudo authorization.
Load the environment in your shell before running any command below:

```bash
set -a
source event-streaming/.env
set +a
```

## Run all

Runs CPU and MPS with OCR PDF, non-OCR PDF, and text:

```bash
python -m event-streaming.scripts.automation
```

## Run three CPU cases

```bash
python -m event-streaming.scripts.automation \
  --environment cpu \
  --file-type pdf_ocr pdf_non_ocr text
```

## Run one case

```bash
python -m event-streaming.scripts.automation --environment cpu --file-type text
```

Use `--environment mps` for MPS. Defaults: 10 events per PDF case, 100 per text case.
