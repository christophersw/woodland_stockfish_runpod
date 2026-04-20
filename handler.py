"""
RunPod Serverless Worker — Stockfish Analysis Handler
=====================================================
Receives a job with a PGN string and game_id, runs Stockfish analysis via the
existing stockfish_pipeline.services.stockfish_service module, and writes
results directly to PostgreSQL using the same schema as the Railway worker.

Expected job["input"]:
{
    "game_id": str,      # Primary key of Game row (string, e.g. chess.com URL slug)
    "pgn":     str,      # Full PGN string of the game
    "depth":   int,      # Optional — overrides ANALYSIS_DEPTH env var
    "threads": int,      # Optional — overrides ANALYSIS_THREADS env var
    "hash_mb": int       # Optional — overrides ANALYSIS_HASH_MB env var
}

Returns:
{
    "game_id":        str,
    "moves_analysed": int,
    "accuracy_white": float,
    "accuracy_black": float,
    "status":         "ok" | "error",
    "error":          str   # only present when status == "error"
}
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import runpod
import sqlalchemy.exc
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

# Analysis logic from the copied / pip-installed pipeline package
from stockfish_pipeline.services.stockfish_service import analyze_pgn
from stockfish_pipeline.storage.models import (
    AnalysisJob,
    Game,
    GameAnalysis,
    GameParticipant,
    MoveAnalysis,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — set these as environment variables in the RunPod endpoint
# ---------------------------------------------------------------------------
STOCKFISH_PATH: str = os.environ.get("STOCKFISH_PATH", "/usr/games/stockfish")
ANALYSIS_DEPTH: int = int(os.environ.get("ANALYSIS_DEPTH", "20"))
ANALYSIS_THREADS: int = int(os.environ.get("ANALYSIS_THREADS", "8"))
ANALYSIS_HASH_MB: int = int(os.environ.get("ANALYSIS_HASH_MB", "2048"))
DATABASE_URL: str = os.environ["DATABASE_URL"]  # Required — raises KeyError if missing

# ---------------------------------------------------------------------------
# DB setup — module-level so the connection pool is reused across warm calls
# ---------------------------------------------------------------------------
_engine = create_engine(DATABASE_URL, pool_pre_ping=True)
_SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


def _save_analysis(session, game_id: str, result) -> None:
    """
    Persist GameAnalysis + MoveAnalysis rows and update GameParticipant stats.

    Mirrors _save_analysis() in analysis_worker.py so the output schema is
    identical whether analysis ran locally or on RunPod.
    """
    ga = session.execute(
        select(GameAnalysis).where(GameAnalysis.game_id == game_id)
    ).scalar_one_or_none()

    if ga is None:
        ga = GameAnalysis(game_id=game_id)
        session.add(ga)
        session.flush()  # populate ga.id before inserting child rows

    ga.analyzed_at = result.analyzed_at
    ga.engine_depth = result.engine_depth
    ga.white_accuracy = result.white_stats.accuracy
    ga.black_accuracy = result.black_stats.accuracy
    ga.white_acpl = result.white_stats.acpl
    ga.black_acpl = result.black_stats.acpl
    ga.white_blunders = result.white_stats.blunders
    ga.white_mistakes = result.white_stats.mistakes
    ga.white_inaccuracies = result.white_stats.inaccuracies
    ga.black_blunders = result.black_stats.blunders
    ga.black_mistakes = result.black_stats.mistakes
    ga.black_inaccuracies = result.black_stats.inaccuracies
    if result.moves:
        ga.summary_cp = result.moves[-1].cp_eval

    # Idempotent — delete any existing move rows before re-inserting
    for old in list(ga.moves):
        session.delete(old)
    session.flush()

    for mr in result.moves:
        session.add(
            MoveAnalysis(
                analysis_id=ga.id,
                ply=mr.ply,
                san=mr.san,
                fen=mr.fen,
                cp_eval=mr.cp_eval,
                best_move=mr.best_move,
                arrow_uci=mr.arrow_uci,
                cpl=mr.cpl,
                classification=mr.classification,
            )
        )

    # Update per-participant stats (quality score, ACPL, counts)
    game = session.get(Game, game_id)
    if game:
        for participant in game.participants:
            color = participant.color.lower()
            if color == "white":
                stats = result.white_stats
            elif color == "black":
                stats = result.black_stats
            else:
                continue
            participant.quality_score = stats.accuracy
            participant.acpl = stats.acpl
            participant.blunder_count = stats.blunders
            participant.mistake_count = stats.mistakes
            participant.inaccuracy_count = stats.inaccuracies


def _mark_job_completed(session, game_id: str, runpod_job_id: str) -> None:
    """Mark the matching AnalysisJob row as completed."""
    job = session.execute(
        select(AnalysisJob).where(
            AnalysisJob.game_id == game_id,
            AnalysisJob.runpod_job_id == runpod_job_id,
        )
    ).scalar_one_or_none()

    if job:
        job.status = "completed"
        job.completed_at = datetime.now(timezone.utc)
        if job.submitted_at:
            elapsed = job.completed_at - job.submitted_at.replace(tzinfo=timezone.utc)
            job.duration_seconds = elapsed.total_seconds()


def handler(job: dict) -> dict:
    """
    RunPod job handler — called once per job by the RunPod SDK.
    All exceptions from the analysis itself are caught and returned as errors
    so RunPod does not retry on bad data.  Transient DB failures are re-raised
    so RunPod's retry policy applies.
    """
    job_input = job["input"]
    game_id: str = job_input["game_id"]
    pgn_string: str = job_input["pgn"]
    depth: int = int(job_input.get("depth", ANALYSIS_DEPTH))
    threads: int = int(job_input.get("threads", ANALYSIS_THREADS))
    hash_mb: int = int(job_input.get("hash_mb", ANALYSIS_HASH_MB))
    runpod_job_id: str = job.get("id", "")

    log.info(
        "Starting analysis: game_id=%s depth=%d threads=%d hash_mb=%d",
        game_id, depth, threads, hash_mb,
    )

    # --- Run analysis (permanent errors caught here) ---
    try:
        result = analyze_pgn(
            pgn_text=pgn_string,
            stockfish_path=STOCKFISH_PATH,
            depth=depth,
            threads=threads,
            hash_mb=hash_mb,
        )
    except Exception as exc:
        log.error("Analysis failed for game_id=%s: %s", game_id, exc, exc_info=True)
        return {"game_id": game_id, "status": "error", "error": str(exc)}

    # --- Write to DB (transient errors re-raised for RunPod retry) ---
    try:
        with _SessionLocal() as session:
            _save_analysis(session, game_id, result)
            _mark_job_completed(session, game_id, runpod_job_id)
            session.commit()
    except sqlalchemy.exc.OperationalError:
        raise  # Transient — let RunPod retry
    except Exception as exc:
        log.error("DB write failed for game_id=%s: %s", game_id, exc, exc_info=True)
        return {"game_id": game_id, "status": "error", "error": str(exc)}

    log.info(
        "Completed: game_id=%s moves=%d acc_w=%.1f acc_b=%.1f",
        game_id, len(result.moves),
        result.white_stats.accuracy, result.black_stats.accuracy,
    )

    return {
        "game_id": game_id,
        "moves_analysed": len(result.moves),
        "accuracy_white": result.white_stats.accuracy,
        "accuracy_black": result.black_stats.accuracy,
        "status": "ok",
    }


# Entry point — RunPod SDK reads test_input.json locally or accepts queue jobs
runpod.serverless.start({"handler": handler})
