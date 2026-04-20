FROM python:3.11-slim

# Install system dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget tar \
    && rm -rf /var/lib/apt/lists/*

# Install Stockfish 18 AVX2 — optimised for modern x86-64 CPUs (RunPod compute nodes)
RUN wget -q "https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar" \
        -O /tmp/stockfish.tar \
    && tar -xf /tmp/stockfish.tar -C /tmp \
    && find /tmp -name "stockfish*" -type f -perm /111 | head -1 \
         | xargs -I{} mv {} /usr/games/stockfish \
    && chmod +x /usr/games/stockfish \
    && rm -f /tmp/stockfish.tar

ENV STOCKFISH_PATH=/usr/games/stockfish \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install Python dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the RunPod handler and the analysis pipeline package
COPY handler.py .
COPY stockfish_pipeline/ ./stockfish_pipeline/

CMD ["python", "handler.py"]
