# Batching benchmarks

Run commands from the repository root.

## Automated benchmarks

> Note: Automated performance tests currently run on macOS only. GPU collection uses built-in
> `ioreg`.

Requires Colima running, Ollama installed, and `batching/.env` configured with 
`OPENSEARCH_PASSWORD` and `DOCPIPE_API_BASE_URL` (see `.env.example`).

### Setup

```bash
cp batching/.env.example batching/.env
uv sync --locked --extra mps
source .venv/bin/activate
```

### Run all six CPU and MPS benchmark cases

Runs CPU and MPS with OCR PDF, non-OCR PDF, and text:

```bash
python -m batching.scripts.automation
```

### Run selected benchmark cases

Choose one or more values for each option. The script runs every selected environment and file-type combination.

```bash
python -m batching.scripts.automation \
  --environment <cpu|mps> [<cpu|mps> ...] \
  --file-type <pdf_ocr|pdf_non_ocr|text> [<pdf_ocr|pdf_non_ocr|text> ...]
```

Environments: `cpu`, `mps`. File types: `pdf_ocr`, `pdf_non_ocr`, `text`.
