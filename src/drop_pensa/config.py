"""
Configuration for drop-pensa, loaded from environment variables.
"""
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings from environment."""

    # Service
    app_name: str = "drop-pensa"
    debug: bool = False

    # Storage
    storage_dir: Path = Path("/var/lib/drop-pensa/files")
    db_url: str = "sqlite:////var/lib/drop-pensa/drop.db"

    # File limits
    max_file_size_mb: int = 50
    default_ttl_seconds: int = 3600  # 1 hour
    max_ttl_seconds: int = 604800  # 7 days

    # Security
    blocked_extensions: str = "exe,bat,cmd,scr,msi,dll,com,js,vbs,jar,apk"
    
    # Cleanup
    cleanup_enabled: bool = True
    cleanup_interval_seconds: int = 300  # 5 minutes

    # Redis (optional)
    redis_url: str | None = None

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def blocked_extensions_set(self) -> set[str]:
        """Return blocked extensions as a lowercase set."""
        return {ext.strip().lower() for ext in self.blocked_extensions.split(",")}

    @property
    def max_file_size_bytes(self) -> int:
        """Return max file size in bytes."""
        return self.max_file_size_mb * 1024 * 1024


# Global settings instance
settings = Settings()
