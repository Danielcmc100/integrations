from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+asyncpg://integrations:integrations@localhost:5432/integrations"
    )
    redis_url: str = "redis://localhost:6379/0"
    github_webhook_secret: str = ""
    plane_webhook_secret: str = ""

    plane_api_token: str = ""
    plane_workspace: str = ""
    plane_base_url: str = "https://api.plane.so/api/v1"

    github_app_id: str = ""
    github_app_private_key: str = ""
    github_app_installation_id: int | None = None
    github_api_base_url: str = "https://api.github.com"

    plane_app_url: str = "https://app.plane.so"
    github_bot_login: str = ""


settings = Settings()
