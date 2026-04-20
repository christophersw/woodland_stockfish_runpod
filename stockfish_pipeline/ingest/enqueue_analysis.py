"""Enqueue analysis jobs for games that have not yet been analyzed by Stockfish."""
from __future__ import annotations

from sqlalchemy import select, func, and_

from stockfish_pipeline.storage.database import get_session, init_db
from stockfish_pipeline.storage.models import AnalysisJob, Game, GameAnalysis


def enqueue_unanalyzed(depth: int = 20, priority: int = 0, limit: int | None = None) -> int:
    """
    Create pending AnalysisJob rows for every game that:
      - has a non-empty PGN, and
      - has no completed AnalysisJob at the requested depth (or higher), and
      - has no real Stockfish analysis yet (game_analysis.analyzed_at IS NULL)

    Returns the number of jobs created.
    """
    init_db()
    with get_session() as session:
        # Sub-query: game_ids that already have a completed job at >= depth
        done_subq = (
            select(AnalysisJob.game_id)
            .where(
                and_(
                    AnalysisJob.status == "completed",
                    AnalysisJob.depth >= depth,
                )
            )
            .scalar_subquery()
        )

        # Sub-query: game_ids that have a pending/running job already
        queued_subq = (
            select(AnalysisJob.game_id)
            .where(AnalysisJob.status.in_(["pending", "running"]))
            .scalar_subquery()
        )

        # Sub-query: game_ids with real analysis (analyzed_at set)
        analyzed_subq = (
            select(GameAnalysis.game_id)
            .where(GameAnalysis.analyzed_at.isnot(None))
            .scalar_subquery()
        )

        stmt = (
            select(Game.id)
            .where(
                and_(
                    Game.pgn != "",
                    Game.pgn.isnot(None),
                    Game.id.not_in(done_subq),
                    Game.id.not_in(queued_subq),
                    Game.id.not_in(analyzed_subq),
                )
            )
            .order_by(Game.played_at.desc())
        )
        if limit:
            stmt = stmt.limit(limit)

        rows = session.execute(stmt).scalars().all()

        jobs = [
            AnalysisJob(game_id=gid, status="pending", priority=priority, depth=depth)
            for gid in rows
        ]
        session.add_all(jobs)
        session.commit()
        return len(jobs)


def queue_status() -> dict:
    """Return counts of jobs by status."""
    init_db()
    with get_session() as session:
        rows = session.execute(
            select(AnalysisJob.status, func.count().label("n"))
            .group_by(AnalysisJob.status)
        ).all()
        return {r.status: r.n for r in rows}
