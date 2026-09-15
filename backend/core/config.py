from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    database_path: Path
    busy_timeout_ms: int = 5000
    app_name: str = "fund-advice"

    @classmethod
    def from_env(cls) -> "Settings":
        configured = os.environ.get("FUND_ADVICE_DATABASE_PATH", "runtime/database/app.sqlite3")
        return cls(database_path=Path(configured).expanduser())
