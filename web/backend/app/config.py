from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:password@localhost:5432/leadcrm"
    SECRET_KEY: str = "change-this-to-a-random-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    RATE_LIMIT_PER_HOUR: int = 25
    CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]
    PARSER_DIR: str = "/media/DATA2/ALL_PROJECT/parser"
    UPLOAD_DIR: str = "./uploads/portfolio"
    PORTFOLIO_URL: str = "http://localhost:5173/portfolio"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
