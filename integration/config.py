from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://integrations:integrations@localhost:5432/integrations"
    )
    redis_url: str = "redis://localhost:6379/0"
    github_webhook_secret: str = ""
    plane_webhook_secret: str = ""


settings = Settings()
