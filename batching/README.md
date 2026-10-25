# Batching benchmarks

Run commands from the repository root.

## Build the CPU image

Build the image used by both CPU and MPS benchmarks in Colima. The sibling
`docling-pipelines` checkout supplies the build context.

```bash
docker --context colima build \
  --platform linux/amd64 \
  -f batching/Dockerfile \
  --build-arg COMPUTE_BACKEND=cpu \
  -t kevin1kurian/docling-pipelines:cpu \
  ../docling-pipelines
```

## Automated benchmarks

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

- Environment values: `cpu`, `mps`.
- File type values: `pdf_ocr`, `pdf_non_ocr`, `text`.

Single environment and file type:

```bash
python -m batching.scripts.automation \
  --environment <environment> \
  --file-type <file_type>
```

Multiple environments and file types:

```bash
python -m batching.scripts.automation \
  --environment <environment_1> <environment_2> \
  --file-type <file_type_1> <file_type_2>
```
