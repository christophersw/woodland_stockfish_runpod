#!/bin/bash
# build-and-push-runpod-image.sh
# Automates building and pushing the woodland-chess-runpod Docker image

set -e

DOCKER_USERNAME="${1:-}"
IMAGE_NAME="woodland-chess-runpod"
IMAGE_TAG="latest"

if [ -z "$DOCKER_USERNAME" ]; then
    echo "Usage: $0 <docker-username>"
    echo "Example: $0 christophersw"
    exit 1
fi

FULL_IMAGE="$DOCKER_USERNAME/$IMAGE_NAME:$IMAGE_TAG"

echo "========================================="
echo "Building woodland-chess-runpod image"
echo "========================================="
echo "Docker Hub username: $DOCKER_USERNAME"
echo "Full image name: $FULL_IMAGE"
echo ""

cd "$(dirname "$0")"

if [ ! -f "Dockerfile" ]; then
    echo "ERROR: Dockerfile not found in $(pwd)"
    exit 1
fi

if [ ! -d "stockfish_pipeline" ]; then
    echo "ERROR: stockfish_pipeline/ not found. Did you run:"
    echo "  cp -r ../woodland_stockfish/stockfish_pipeline ."
    exit 1
fi

echo "✓ Prerequisites OK"
echo ""

# Build
echo "Building Docker image (this may take 5–10 minutes)..."
docker build -t "$FULL_IMAGE" .

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Image built successfully: $FULL_IMAGE"
else
    echo ""
    echo "✗ Docker build failed"
    exit 1
fi

echo ""
echo "========================================="
echo "Pushing to Docker Hub"
echo "========================================="
echo ""
echo "Make sure you're logged in to Docker Hub:"
echo "  docker login"
echo ""
read -p "Press Enter to continue (or Ctrl+C to cancel)..."

docker push "$FULL_IMAGE"

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Image pushed successfully!"
    echo ""
    echo "Next steps:"
    echo "  1. Go to RunPod Dashboard: https://www.runpod.io/console/serverless"
    echo "  2. Click 'Create New' Serverless Endpoint"
    echo "  3. Container image: $FULL_IMAGE"
    echo "  4. CPU type: Compute Optimized"
    echo "  5. Min workers: 0, Max workers: 10, Idle timeout: 5s"
    echo "  6. Add environment variables:"
    echo "     - DATABASE_URL=postgresql://user:pass@host/db"
    echo "     - STOCKFISH_PATH=/usr/games/stockfish"
    echo "     - ANALYSIS_DEPTH=20"
    echo "     - ANALYSIS_THREADS=8"
    echo "     - ANALYSIS_HASH_MB=2048"
    echo "  7. Deploy and note the Endpoint ID"
    echo ""
    echo "Then in Railway (woodland_stockfish service), add:"
    echo "  - RUNPOD_ENDPOINT_ID=<from step 7>"
    echo "  - RUNPOD_API_KEY=<from RunPod dashboard>"
    echo ""
else
    echo ""
    echo "✗ Docker push failed"
    exit 1
fi
