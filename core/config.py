from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field

class Settings(BaseSettings):
    app_env: str = Field(default="dev", alias="APP_ENV")
    app_base_url: str = Field(default="http://localhost:8000", alias="APP_BASE_URL")
    
    # AI Config
    alem_api_key: str = Field(default="sk-YyCsvojyayk8wjNiEcF8tg", alias="ALEM_API_KEY")
    alem_api_url: str = Field(default="https://llm.alem.ai/v1/chat/completions", alias="ALEM_API_URL")
    alem_model: str = Field(default="qwen3-6", alias="ALEM_MODEL")
    ocr_api_key: str = Field(default="sk-j-lrw4OFHBVXeF174HRszg", alias="OCR_API_KEY")
    
    # Kaspi Config
    kaspi_pos_url: str = Field(default="http://100.75.211.119:4001", alias="KASPI_POS_URL")
    kaspi_api_key: str = Field(default="astanagb-kaspi-key", alias="KASPI_API_KEY")
    kaspi_wh_secret: str = Field(default="b8daafada57acef22720443606cacb441bc4bd0228b6374f627a8b75d474edf0", alias="KASPI_WH_SECRET")
    
    # Admin Config
    admin_phone: str = Field(default="77064177628", alias="ADMIN_PHONE")
    admin_registration_key: str = Field(default="", alias="ADMIN_REGISTRATION_KEY")
    
    # Auth & OAuth Config
    google_client_id: str = Field(default="", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field(default="", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field(default="", alias="GOOGLE_REDIRECT_URI")
    allow_guest_login: str = Field(default="0", alias="ALLOW_GUEST_LOGIN")
    auth_password_min_length: int = Field(default=8, alias="AUTH_PASSWORD_MIN_LENGTH")
    
    # Email Config
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    email_from: str = Field(default="", alias="EMAIL_FROM")

    # DB Config
    database_url: str = Field(default="sqlite+aiosqlite:///lendings.db", alias="DATABASE_URL")

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
