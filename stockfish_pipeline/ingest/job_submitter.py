"""
job_submitter.py — Submits pending AnalysisJob rows to the RunPod Serverless endpoint.

Replaces the local Stockfish worker loop (run_analysis_worker.py) when
RUNPOD_ENDPOINT_ID is set.  The RunPod worker writes results directly to
PostgreSQL, so this process is fire-and-forget: it submits jobs and moves on.

Environment variables (all required unless noted):
    RUNPOD_ENDPOINT_ID  — Endpoint ID from the RunPod dashboard
    RUNPOD_API_KEY      — API key from the RunPod dashboard
    DATABASE_URL        — PostgreSQL connection string
    ANALYSIS_DEPTH      — (optional) Stockfish search depth forwarded to worker (default: 20)
    ANALYSIS_THREADS    — (optional) Threads forwarded to worker (default: 8)
    ANALYSIS_HASH_MB    — (optional) Hash MB forwarded to worker (default: 2048)
    SF_POLL_INTERVAL    — (optional) Seconds between submission sweeps (default: 60)
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone

import runpod
from sqlalchemy import and_, select

from stockfish_pipeline.storage.database import get_session, init_db
from stockfish_pipeline.storage.models import AnalysisJob, Game

log = logging.getLogger(__name__)

RUNPOD_ENDPOINT_ID: str = os.environ["RUNPOD_ENDPOINT_ID"]
RUNPOD_API_KEY: str = os.environ["RUNPOD_API_KEY"]
ANALYSIS_DEPTH: int = int(os.environ.get("ANALYSIS_DEPTH", "20"))
ANALYSIS_THREADS: int = int(os.environ.get("ANALYSIS_THREADS", "8"))
ANALYSIS_HASH_MB: int = int(os.environ.get("ANALYSIS_HASH_MB", "2048"))
POLL_INTERVAL: int = int(os.environ.get("SF_POLL_INTERVAL", "60"))

runpod.api_key = RUNPOD_API_KEY
_endpoint = runpod.Endpoint(RUNPOD_ENDPOINT_ID)


def _load_pgn(game_id: str) -> str:
    """Load PGN for a game_id in a short-lived session."""
    with get_session() as session:
        game = session.get(Game, game_id)
        return game.pgn if game and game.pgn else ""


def submit_pending_jobs(limit: int | None = None) -> int:
    """
    Query for pending AnalysisJob rows and submit each to RunPod.

    Updates status to "submitted" and stores the RunPod job ID.
    Returns the number of jobs successfully submitted.
    """
    stmt = (
        select(AnalysisJob)
        .where(
            and_(
                AnalysisJob.status == "pending",
                AnalysisJob.engine == "stockfish",
            )
        )
        .order_by(AnalysisJob.priority.desc(), AnalysisJob.created_at)
    )
    if limit:
        stmt = stmt.limit(limit)

    submitted = 0
    with get_session() as session:
        jobs = session.execute(stmt).scalars().all()

        for job in jobs:
            pgn = _load_pgn(job.game_id)
            if not pgn:
                log.warning("game_id=%s has no PGN — skipping", job.game_id)
                continue

            try:
                run_request = _endpoint.run(
                    {
                        "game_id": job.game_id,
                        "pgn": pgn,
                        "depth": job.depth,
                        "threads": ANALYSIS_THREADS,
                        "hash_mb": ANALYSIS_HASH_MB,
                    }
                )
                job.runpod_job_id = run_request.job_id
                job.submitted_at = datetime.now(timezone.utc)
                job.status = "submitted"
                log.info(
                    "Submitted game_id=%s → runpod_job_id=%s",
                    job.game_id,
                    run_request.job_id,
                )
                submitted += 1
            except Exception:
                log.exception("Failed to submit game_id=%s", job.game_id)

        session.commit()

    return submitted


def run_submitter_loop() -> None:
    """Continuously submit new pending AnalysisJob rows to RunPod."""
    init_db()
    log.info(
        "Job submitter started — endpoint=%s, poll_interval=%ds",
        RUNPOD_ENDPOINT_ID,
        POLL_INTERVAL,
    )
    while True:
        try:
            n = submit_pending_jobs()
            log.info("Submitted %d job(s). Sleeping %ds.", n, POLL_INTERVAL)
        except Exception:
            log.exception("Unexpected error in submission sweep — will retry")
        time.sleep(POLL_INTERVAL)
