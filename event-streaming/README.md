# Event streaming

## Setup

```sh
colima start
uv sync --locked --all-extras
cp -n event-streaming/.env.example event-streaming/.env
```

## Build

```sh
docker --context colima build --platform linux/amd64 -f event-streaming/Dockerfile \
  --build-arg COMPUTE_BACKEND=cpu \
  -t "kevin1kurian/docling-pipelines:v2.0.1-cpu-streaming-<date>" ../docling-pipelines
```

## Environment

```sh
source .venv/bin/activate
set -a
. event-streaming/.env
set +a
export PYTHONPATH=event-streaming
```

## CPU / MPS

MPS automation starts native Ollama on port `11435` to avoid conflicts with an existing Ollama server on port `11434`.

```sh
python -m scripts.automation --environment cpu --type ocr
```

### Options

| Option           | Value                                    |
|------------------|------------------------------------------|
| `--environment`  | `cpu`, `mps`; required                   |
| `--type`         | `ocr`, `non-ocr`, `text`; required       |
| `--results-root` | Path; default: `event-streaming/results` |
| `--help`         |                                          |

## Suite

```sh
python -m scripts.automation --suite all
```

### Options

| Option           | Value                                                   |
|------------------|---------------------------------------------------------|
| `--suite`        | `cpu`, `mps`, `all`, `non-ocr`, `ocr`, `text`; required |
| `--results-root` | Path; default: `event-streaming/results`                |
| `--help`         |                                                         |

## ToDo
- Add Run Name/Details to Results HTML 
- Organize Scripts Package
- Add Consolidate Results HTML for Suit Runs
- Cleanup Batching using the New Scripts Structure
- Add Resource Monitoring in Scripts
