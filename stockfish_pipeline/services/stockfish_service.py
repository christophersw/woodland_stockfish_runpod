"""Stockfish analysis service using python-chess chess.engine."""
from __future__ import annotations

import io
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

import chess
import chess.engine
import chess.pgn

from stockfish_pipeline.config import get_settings

# Classification thresholds (centipawn loss from mover's perspective)
# Matches Lichess: https://lichess.org/page/accuracy
_BLUNDER_CPL = 300
_MISTAKE_CPL = 100
_INACCURACY_CPL = 50

# Brilliant/great move detection thresholds
# Brilliant: sacrifices material, move is best (CPL < 10), and position was not already clearly winning
# Great: only-good-move in a difficult position (narrow margin, CPL < 10 but alternatives are bad)
_BRILLIANT_MAX_CPL = 10          # must be (near-)best to earn !!
_BRILLIANT_WIN_CEIL = 70.0       # don't award !! when already clearly winning (>70% win chance)
_BRILLIANT_ALT_FLOOR = 150.0    # second-best alternative must be ≥150 cp worse to qualify
_GREAT_MAX_CPL = 10              # must be (near-)best for !
_GREAT_ALT_FLOOR = 80.0         # only-good-move: alternatives ≥80 cp worse


@dataclass
class MoveResult:
    ply: int
    san: str
    fen: str
    cp_eval: float        # eval after the move was played (white-relative, centipawns)
    best_move: str        # UCI of the engine's top choice before this move
    arrow_uci: str        # same as best_move (consumed by the board UI)
    cpl: float            # centipawn loss for the side that just moved (≥ 0)
    classification: str   # brilliant / great / best / excellent / good / inaccuracy / mistake / blunder


@dataclass
class PlayerStats:
    accuracy: float
    acpl: float
    blunders: int
    mistakes: int
    inaccuracies: int


@dataclass
class GameResult:
    white_stats: PlayerStats
    black_stats: PlayerStats
    moves: list[MoveResult]
    engine_depth: int
    analyzed_at: datetime


def _cp(score: chess.engine.Score) -> float:
    """Convert a Score to white-relative centipawns, preserving mate distance."""
    if score.is_mate():
        encoded = score.score(mate_score=10000)
        if encoded is not None:
            return float(encoded)
        mate = score.mate()
        return 10000.0 if (mate is not None and mate > 0) else -10000.0
    val = score.score()
    return float(val) if val is not None else 0.0


def _win_percent(cp: float) -> float:
    """Win percentage (0–100) from a subjective centipawn eval.

    Uses the Lichess empirical sigmoid derived from 2300+ rated games.
    See https://github.com/lichess-org/lila/pull/11148
    """
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def _move_accuracy(wp_before: float, wp_after: float) -> float:
    """Per-move accuracy from Win% before and after (both on 0–100 scale).

    Lichess formula with +1 uncertainty bonus for imperfect analysis depth.
    See https://lichess.org/page/accuracy
    """
    if wp_after >= wp_before:
        return 100.0
    win_diff = wp_before - wp_after
    raw = 103.1668100711649 * math.exp(-0.04354415386753951 * win_diff) - 3.166924740191411 + 1
    return max(0.0, min(100.0, raw))


def _harmonic_mean(values: list[float]) -> float:
    """Harmonic mean, safe for near-zero values."""
    if not values:
        return 0.0
    eps = 0.001
    return len(values) / sum(1.0 / max(v, eps) for v in values)


def _weighted_mean(values: list[float], weights: list[float]) -> float:
    """Weighted arithmetic mean."""
    if not values:
        return 0.0
    total_weight = sum(weights)
    if total_weight == 0:
        return sum(values) / len(values)
    return sum(v * w for v, w in zip(values, weights)) / total_weight


def _game_accuracy(move_accs: list[float], win_percents: list[float]) -> float:
    """Game-level accuracy: (volatility-weighted mean + harmonic mean) / 2.

    Matches Lichess AccuracyPercent.scala — sliding window std-dev weights
    emphasise moves played in volatile positions.
    See https://github.com/lichess-org/lila/blob/master/modules/analyse/src/main/AccuracyPercent.scala
    """
    n = len(move_accs)
    if n == 0:
        return 100.0
    if n == 1:
        return move_accs[0]

    window_size = max(2, min(8, n // 10))

    # Build one weight per move using the std-dev of a sliding window of win%s.
    # Pad the front so every move gets a window of the same logical size.
    weights: list[float] = []
    for i in range(n):
        start = max(0, i - window_size + 1)
        window = win_percents[start : i + 1]
        if len(window) < 2:
            weights.append(0.5)
        else:
            sd = statistics.stdev(window)
            weights.append(max(0.5, min(12.0, sd)))

    harmonic = _harmonic_mean(move_accs)
    weighted = _weighted_mean(move_accs, weights)
    return (harmonic + weighted) / 2.0


def _classify(
    cpl: float,
    wp_before: float,
    wp_after: float,
    best_cp_before: float,
    second_cp_before: float | None,
    is_capture: bool,
) -> str:
    """Classify a move.

    brilliant (!!): near-best material sacrifice, not already clearly winning.
    great    (!):  only-good-move in a difficult position.
    best:          CPL < 10, neither brilliant nor great.
    excellent:     CPL 10–49.
    good:          (reserved for future use / removed from this scale)
    inaccuracy:    CPL 50–99.
    mistake:       CPL 100–299.
    blunder:       CPL ≥ 300.
    """
    if cpl >= _BLUNDER_CPL:
        return "blunder"
    if cpl >= _MISTAKE_CPL:
        return "mistake"
    if cpl >= _INACCURACY_CPL:
        return "inaccuracy"

    alt_cpl = (best_cp_before - second_cp_before) if second_cp_before is not None else 0.0

    if cpl < _BRILLIANT_MAX_CPL and is_capture and wp_before < _BRILLIANT_WIN_CEIL and alt_cpl >= _BRILLIANT_ALT_FLOOR:
        return "brilliant"

    if cpl < _GREAT_MAX_CPL and second_cp_before is not None and alt_cpl >= _GREAT_ALT_FLOOR:
        return "great"

    if cpl < _BRILLIANT_MAX_CPL:
        return "best"
    return "excellent"


def analyze_pgn(
    pgn_text: str,
    stockfish_path: str,
    depth: int = 20,
    threads: int = 1,
    hash_mb: int = 256,
    syzygy_path: str | None = None,
    move_callback: "callable[[int, int, str], None] | None" = None,
) -> GameResult:
    """Analyze a full game PGN and return per-move results plus player stats.

    move_callback(ply, total_moves, san) is called after each move is analyzed.
    """
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise ValueError("Could not parse PGN")

    # Count total moves up front so callers can show a denominator
    total_moves = sum(1 for _ in game.mainline_moves())

    engine_options: dict = {"Threads": str(threads), "Hash": str(hash_mb)}
    if syzygy_path:
        engine_options["SyzygyPath"] = syzygy_path
    limit = chess.engine.Limit(depth=depth)

    move_results: list[MoveResult] = []
    white_move_accs: list[float] = []
    black_move_accs: list[float] = []
    white_cpls: list[float] = []
    black_cpls: list[float] = []
    white_wps: list[float] = []   # mover-relative win% after each white move
    black_wps: list[float] = []   # mover-relative win% after each black move

    # Collect all positions in one pass so we can analyse each board exactly once.
    # Each position's pre-move eval doubles as the previous position's post-move eval.
    # This cuts engine calls from ~3N to N+1 (one multipv=2 call per position, plus
    # one call for the final position to get the last move's post-move eval).
    mainline: list[tuple[chess.Board, chess.Move, str, int]] = []
    _scan_board = game.board()
    for node in game.mainline():
        move = node.move
        ply = _scan_board.ply() + 1
        san = _scan_board.san(move)
        mainline.append((_scan_board.copy(), move, san, ply))
        _scan_board.push(move)
    final_board = _scan_board  # position after the last move

    # Analyse every pre-move position once (multipv=2) plus the final position.
    # pos_infos[i] corresponds to mainline[i]'s pre-move board; pos_infos[N] is the final board.
    # This gives each move's "after" eval for free: it equals pos_infos[i+1].best_cp.
    # Net result: N+1 engine calls instead of ~3N.
    def _analyse_board(engine: chess.engine.SimpleEngine, board: chess.Board, multipv: int = 1) -> tuple[float, float | None, str]:
        """Return (best_cp_white, second_cp_white_or_None, best_move_uci_str)."""
        result = engine.analyse(board, limit, multipv=multipv)
        if isinstance(result, list):
            top = result[0]
            second_cp: float | None = _cp(result[1]["score"].white()) if len(result) > 1 else None
        else:
            top = result
            second_cp = None
        best_cp = _cp(top["score"].white())
        best_uci = top.get("pv", [None])[0]
        return best_cp, second_cp, best_uci.uci() if best_uci else ""

    with chess.engine.SimpleEngine.popen_uci(stockfish_path) as engine:
        engine.configure(engine_options)

        # First pass: analyse every position with multipv=1 (fast path).
        # pos_infos[i] = pre-move eval for mainline[i]; pos_infos[N] = final position eval.
        pos_infos: list[tuple[float, float | None, str]] = []
        boards_to_analyse = [b for b, _, _, _ in mainline] + [final_board]
        for i, board in enumerate(boards_to_analyse):
            pos_infos.append(_analyse_board(engine, board, multipv=1))
            if move_callback and i < len(mainline):
                _, _, san, ply = mainline[i]
                move_callback(ply, total_moves, san)

        # Pair pre/post evals and build results.
        for idx, (board, move, san, ply) in enumerate(mainline):
            best_cp_before, _, best_move_str = pos_infos[idx]
            after_cp = pos_infos[idx + 1][0]

            is_white_move = board.turn == chess.WHITE
            is_capture = board.is_capture(move)
            legal_moves = list(board.legal_moves)

            board_after = board.copy()
            board_after.push(move)

            if len(legal_moves) == 1:
                move_results.append(MoveResult(
                    ply=ply,
                    san=san,
                    fen=board_after.fen(),
                    cp_eval=after_cp,
                    best_move=move.uci(),
                    arrow_uci=move.uci(),
                    cpl=0.0,
                    classification="best",
                ))
                continue

            if is_white_move:
                cpl = max(0.0, best_cp_before - after_cp)
            else:
                cpl = max(0.0, after_cp - best_cp_before)

            # Only fetch second-best move when the played move is near-best (CPL < 10),
            # since brilliant/great detection requires it and blunders/mistakes never qualify.
            second_cp_before: float | None = None
            if cpl < _GREAT_MAX_CPL and len(legal_moves) > 1:
                _, second_cp_before, _ = _analyse_board(engine, board, multipv=2)

            if is_white_move:
                second_cp_mover = second_cp_before
            else:
                second_cp_mover = -second_cp_before if second_cp_before is not None else None

            wp_before = _win_percent(best_cp_before if is_white_move else -best_cp_before)
            wp_after = _win_percent(after_cp if is_white_move else -after_cp)
            move_acc = _move_accuracy(wp_before, wp_after)

            best_cp_mover = best_cp_before if is_white_move else -best_cp_before
            classification = _classify(
                cpl=cpl,
                wp_before=wp_before,
                wp_after=wp_after,
                best_cp_before=best_cp_mover,
                second_cp_before=second_cp_mover,
                is_capture=is_capture,
            )

            if is_white_move:
                white_cpls.append(cpl)
                white_move_accs.append(move_acc)
                white_wps.append(wp_after)
            else:
                black_cpls.append(cpl)
                black_move_accs.append(move_acc)
                black_wps.append(wp_after)

            move_results.append(MoveResult(
                ply=ply,
                san=san,
                fen=board_after.fen(),
                cp_eval=after_cp,
                best_move=best_move_str,
                arrow_uci=best_move_str,
                cpl=cpl,
                classification=classification,
            ))

    def _stats(cpls: list[float], move_accs: list[float], wps: list[float]) -> PlayerStats:
        if not cpls:
            return PlayerStats(accuracy=100.0, acpl=0.0, blunders=0, mistakes=0, inaccuracies=0)
        return PlayerStats(
            accuracy=_game_accuracy(move_accs, wps),
            acpl=sum(cpls) / len(cpls),
            blunders=sum(1 for c in cpls if c >= _BLUNDER_CPL),
            mistakes=sum(1 for c in cpls if _MISTAKE_CPL <= c < _BLUNDER_CPL),
            inaccuracies=sum(1 for c in cpls if _INACCURACY_CPL <= c < _MISTAKE_CPL),
        )

    return GameResult(
        white_stats=_stats(white_cpls, white_move_accs, white_wps),
        black_stats=_stats(black_cpls, black_move_accs, black_wps),
        moves=move_results,
        engine_depth=depth,
        analyzed_at=datetime.now(timezone.utc),
    )


def analyse_game(
    game: chess.pgn.Game,
    stockfish_path: str,
    depth: int = 20,
    threads: int = 1,
    hash_mb: int = 256,
    syzygy_path: str | None = None,
) -> GameResult:
    """Analyse a pre-parsed chess.pgn.Game object.

    Convenience wrapper around analyze_pgn() for callers that already hold a
    parsed game (e.g. the RunPod serverless handler).

    Returns the same GameResult as analyze_pgn().
    """
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=False)
    pgn_text = game.accept(exporter)
    return analyze_pgn(
        pgn_text=pgn_text,
        stockfish_path=stockfish_path,
        depth=depth,
        threads=threads,
        hash_mb=hash_mb,
        syzygy_path=syzygy_path,
    )
