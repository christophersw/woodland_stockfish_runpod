# woodland_chess_runpod

RunPod Serverless CPU worker for Stockfish game analysis.

## What it does

Receives a job with a `game_id` and PGN string, runs Stockfish analysis using
the `woodland_stockfish` pipeline logic, and writes results directly to the
shared PostgreSQL database. Scales to zero when idle ($0 cost).

## Local testing

```bash
# Copy the stockfish_pipeline package from woodland_stockfish
cp -r ../woodland_stockfish/stockfish_pipeline .

pip install -r requirements.txt

export DATABASE_URL="postgresql://user:pass@host/db"
export STOCKFISH_PATH="/usr/local/bin/stockfish"
export SYZYGY_PATH="/runpod-volume/syzygy"

# RunPod SDK reads test_input.json and calls handler() without a RunPod account
python handler.py
```

## Docker build

```bash
# Copy pipeline package first
cp -r ../woodland_stockfish/stockfish_pipeline .

docker build -t yourdockerhub/woodland-chess-worker .
docker push yourdockerhub/woodland-chess-worker
```

## Automated Docker Hub publish (GitHub Actions)

This repository uses a 2-step GitHub Actions flow:

1. PR validation workflow (`.github/workflows/docker-pr-build.yml`)
	- Trigger: pull requests to `main`/`master` when Docker-related files change
	- Action: builds the image only (no Docker Hub push)
	- Purpose: catch Docker/build issues before merge

2. Publish workflow (`.github/workflows/docker-publish.yml`)
	- Trigger: pushes to `main`/`master` when Docker-related files change
	- Action: builds and pushes the image to Docker Hub

Watched paths:
- `Dockerfile`
- `requirements.txt`
- `handler.py`
- `stockfish_pipeline/**`
- `.github/workflows/docker-pr-build.yml`
- `.github/workflows/docker-publish.yml`

It publishes two tags:
- `latest`
- short commit SHA (for example: `sha-abc1234`)

Set these GitHub repository secrets before using it:
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN` (Docker Hub access token, not your password)

Both workflows can also be run manually from the Actions tab via `workflow_dispatch`.

## RunPod endpoint settings

| Setting | Value |
|---|---|
| Container image | your Docker Hub image |
| CPU type | Compute Optimized |
| Min workers (Active) | `0` |
| Max workers (Flex) | `10` |
| Idle timeout | `5` seconds |
| Execution timeout | `300` seconds |
| Container disk | `5 GB` |

## Environment variables (set in RunPod dashboard)

| Variable | Description |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `STOCKFISH_PATH` | `/usr/games/stockfish` (default) |
| `ANALYSIS_DEPTH` | `20` (default) |
| `ANALYSIS_THREADS` | `8` (default) |
| `ANALYSIS_HASH_MB` | `2048` (default) |
| `SYZYGY_PATH` | `/runpod-volume/syzygy` (default; folder containing `.rtbw` and `.rtbz` files) |
