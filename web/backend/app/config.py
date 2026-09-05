from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    DATABASE_URL: str
    SECRET_KEY: str = Field(min_length=32)
    ALGORITHM: Literal["HS256"] = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = Field(min_length=12)
    RATE_LIMIT_PER_HOUR: int = 25
    CORS_ORIGINS: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )
    PARSER_DIR: str = str(REPOSITORY_ROOT)
    UPLOAD_DIR: str = str(BACKEND_ROOT / "uploads" / "portfolio")
    PORTFOLIO_URL: str = "http://localhost:5173/portfolio"

    @model_validator(mode="after")
    def reject_placeholder_credentials(self) -> "Settings":
        invalid_secret_values = {
            "change-this-to-a-random-secret-key",
            "generate-a-random-secret-before-starting",
        }
        invalid_password_values = {"admin123", "replace-with-a-unique-password"}
        if self.SECRET_KEY in invalid_secret_values:
            raise ValueError("SECRET_KEY must be replaced before startup")
        if self.ADMIN_PASSWORD in invalid_password_values:
            raise ValueError("ADMIN_PASSWORD must be replaced before startup")
        return self

    model_config = {
        "env_file": (".env", "web/backend/.env"),
        "extra": "ignore",
    }


settings = Settings()
