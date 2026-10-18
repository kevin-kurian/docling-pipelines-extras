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

> The GPU image provides CUDA libraries; the device reservation in `batching/docker-compose-gpu.yml` exposes the host GPUs. This requires NVIDIA drivers and NVIDIA Container Toolkit on the host.
