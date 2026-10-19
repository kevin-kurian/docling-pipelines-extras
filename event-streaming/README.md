# Event Streaming

## Build and push images

From the `docling-pipelines-extras` repository root, run these commands. The `docling-pipelines` and `docling-pipelines-extras` checkouts must be sibling directories. The core repository is passed as the build context so the Dockerfile can copy `src/docpipe/`.

```bash
docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=cpu -f event-streaming/Dockerfile -t kevin1kurian/docling-pipelines:v2.0.1-cpu-streaming ../docling-pipelines
docker push kevin1kurian/docling-pipelines:v2.0.1-cpu-streaming

docker build --platform linux/amd64 --build-arg COMPUTE_BACKEND=gpu -f event-streaming/Dockerfile -t kevin1kurian/docling-pipelines:v2.0.1-gpu-streaming ../docling-pipelines
docker push kevin1kurian/docling-pipelines:v2.0.1-gpu-streaming
```
