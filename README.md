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

This repository includes a workflow at `.github/workflows/docker-publish.yml`
that automatically builds and pushes the image on pushes to `main`/`master`
when Docker-related files change.

Watched paths:
- `Dockerfile`
- `requirements.txt`
- `handler.py`
- `stockfish_pipeline/**`
- `.github/workflows/docker-publish.yml`

It publishes two tags:
- `latest`
- short commit SHA (for example: `sha-abc1234`)

Set these GitHub repository secrets before using it:
- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN` (Docker Hub access token, not your password)

You can also run it manually from the Actions tab via `workflow_dispatch`.

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
