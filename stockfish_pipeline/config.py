from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Stockfish Pipeline"
    database_url: str = ""
    chess_com_usernames: str = ""
    chess_com_user_agent: str = "stockfish-pipeline/0.1"
    ingest_month_limit: int = 24
    stockfish_path: str = ""
    analysis_depth: int = 20
    analysis_threads: int = 1
    analysis_hash_mb: int = 256

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    def chess_usernames(self) -> list[str]:
        if not self.chess_com_usernames.strip():
            return []
        return [u.strip().lower() for u in self.chess_com_usernames.split(",") if u.strip()]


def get_settings() -> Settings:
    return Settings()
