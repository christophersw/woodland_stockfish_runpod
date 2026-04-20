from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import io

import chess.pgn
from sqlalchemy import select

from stockfish_pipeline.config import get_settings
from stockfish_pipeline.ingest.chesscom_client import ChessComClient
from stockfish_pipeline.storage.database import get_session, init_db
from stockfish_pipeline.storage.models import Game, GameParticipant, Player


@dataclass
class SyncStats:
    username: str
    inserted: int = 0
    updated: int = 0
    archives_scanned: int = 0


SyncProgressCallback = Callable[[str, int, int, SyncStats], None]


class ChessComSyncService:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._client = ChessComClient()
        init_db()

    def _archive_in_scope(self, archive_url: str) -> bool:
        limit = self._settings.ingest_month_limit
        if limit <= 0:
            return True

        parts = archive_url.rstrip("/").split("/")
        if len(parts) < 2:
            return True

        try:
            year = int(parts[-2])
            month = int(parts[-1])
            archive_dt = datetime(year, month, 1, tzinfo=UTC)
        except ValueError:
            return True

        now = datetime.now(UTC)
        months_old = (now.year - archive_dt.year) * 12 + (now.month - archive_dt.month)
        return months_old <= limit

    def sync_many(self, usernames: list[str]) -> list[SyncStats]:
        return [self.sync_player(username) for username in usernames]

    def sync_player(self, username: str, progress_callback: SyncProgressCallback | None = None) -> SyncStats:
        username = username.lower().strip()
        stats = SyncStats(username=username)

        with get_session() as session:
            player = session.scalar(select(Player).where(Player.username == username))
            if player is None:
                player = Player(username=username, display_name=username)
                session.add(player)
                session.flush()

            archives = self._client.get_archives(username)
            archives = [a for a in archives if self._archive_in_scope(a)]
            stats.archives_scanned = len(archives)

            if progress_callback is not None:
                progress_callback(username, 0, len(archives), stats)

            for archive_idx, archive_url in enumerate(archives, start=1):
                for payload in self._client.get_games_for_archive(archive_url):
                    changed = self._upsert_game(session, player, payload)
                    if changed == "inserted":
                        stats.inserted += 1
                    elif changed == "updated":
                        stats.updated += 1

                if progress_callback is not None:
                    progress_callback(username, archive_idx, len(archives), stats)

            session.commit()

        return stats

    def _upsert_game(self, session, player: Player, payload: dict) -> str:
        game_id = payload.get("uuid") or self._stable_game_id(payload)
        game = session.get(Game, game_id)
        created = game is None
        if created:
            game = Game(id=game_id)
            session.add(game)

        white = payload.get("white", {})
        black = payload.get("black", {})
        white_user = (white.get("username") or "").lower()
        black_user = (black.get("username") or "").lower()

        if white_user == player.username:
            is_white = True
        elif black_user == player.username:
            is_white = False
        else:
            # Fallback for malformed usernames: keep deterministic perspective.
            is_white = True

        my_side = white if is_white else black
        opp_side = black if is_white else white

        result = self._normalize_result(my_side.get("result", ""))
        played_at = datetime.fromtimestamp(int(payload.get("end_time", 0)), tz=UTC)
        result_pgn = payload.get("pgn", "")
        result_header = self._result_from_pgn(result_pgn)

        opening_name, eco_code = self._opening_from_pgn(result_pgn)

        game.played_at = played_at
        game.time_control = payload.get("time_control", "")
        game.white_username = white_user or None
        game.black_username = black_user or None
        game.white_rating = self._safe_int(white.get("rating"))
        game.black_rating = self._safe_int(black.get("rating"))
        game.result_pgn = result_header
        if result_header == "1-0":
            game.winner_username = white_user or None
        elif result_header == "0-1":
            game.winner_username = black_user or None
        else:
            game.winner_username = None
        game.eco_code = eco_code
        game.opening_name = opening_name
        game.lichess_opening = self._lichess_opening_from_pgn(result_pgn)
        game.pgn = result_pgn

        self._upsert_participant(
            session=session,
            game_id=game_id,
            player=player,
            color=("White" if is_white else "Black"),
            opponent_username=(opp_side.get("username") or "unknown").lower(),
            player_rating=self._safe_int(my_side.get("rating")),
            opponent_rating=self._safe_int(opp_side.get("rating")),
            result=result,
        )

        return "inserted" if created else "updated"

    @staticmethod
    def _upsert_participant(
        session,
        *,
        game_id: str,
        player: Player,
        color: str,
        opponent_username: str,
        player_rating: int | None,
        opponent_rating: int | None,
        result: str,
    ) -> None:
        participant = session.scalar(
            select(GameParticipant).where(
                GameParticipant.game_id == game_id,
                GameParticipant.player_id == player.id,
            )
        )
        if participant is None:
            participant = GameParticipant(game_id=game_id, player_id=player.id)
            session.add(participant)

        participant.color = color
        participant.opponent_username = opponent_username
        participant.player_rating = player_rating
        participant.opponent_rating = opponent_rating
        participant.result = result

    @staticmethod
    def _safe_int(value) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stable_game_id(payload: dict) -> str:
        raw = f"{payload.get('url', '')}|{payload.get('end_time', '')}|{payload.get('pgn', '')[:120]}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _normalize_result(value: str) -> str:
        draw_results = {
            "agreed",
            "repetition",
            "stalemate",
            "insufficient",
            "50move",
            "timevsinsufficient",
        }
        loss_results = {"checkmated", "resigned", "timeout", "lose", "abandoned"}

        if value == "win":
            return "Win"
        if value in draw_results:
            return "Draw"
        if value in loss_results:
            return "Loss"
        return "Draw"

    @staticmethod
    def _result_from_pgn(pgn: str) -> str | None:
        if not pgn.strip():
            return None

        game = chess.pgn.read_game(io.StringIO(pgn))
        if game is None:
            return None

        value = (game.headers.get("Result") or "").strip()
        return value or None

    @staticmethod
    def _opening_from_pgn(pgn: str) -> tuple[str, str]:
        if not pgn.strip():
            return "Unknown", ""

        game = chess.pgn.read_game(io.StringIO(pgn))
        if game is None:
            return "Unknown", ""

        headers = game.headers
        opening_name = headers.get("Opening", "").strip()
        eco = headers.get("ECO", "").strip()

        if opening_name:
            return opening_name, eco

        board = game.board()
        sans: list[str] = []
        for idx, move in enumerate(game.mainline_moves(), start=1):
            sans.append(board.san(move))
            board.push(move)
            if idx >= 5:
                break

        return (" ".join(sans) if sans else "Unknown"), eco

    @staticmethod
    def _lichess_opening_from_pgn(pgn: str) -> str | None:
        """Return the most specific Lichess opening name for a PGN, or None."""
        from stockfish_pipeline.services.opening_book import opening_at_each_ply
        if not pgn or not pgn.strip():
            return None
        plies = opening_at_each_ply(pgn, max_ply=20)
        if not plies:
            return None
        # The last entry is the most specific opening reached.
        eco, name = plies[-1]
        if name and name != "Starting Position":
            return f"{eco} {name}".strip() if eco else name
        return None
