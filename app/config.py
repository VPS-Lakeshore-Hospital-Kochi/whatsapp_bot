"""Runtime configuration, read from environment variables (see .env.example)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- WhatsApp Cloud API (Meta) ---
    wa_access_token: str = ""  # System-user permanent token; empty = dry-run (log only)
    wa_phone_number_id: str = ""
    wa_app_secret: str = ""  # Used to verify X-Hub-Signature-256 on every webhook
    wa_verify_token: str = ""  # Shared secret for Meta's GET webhook handshake
    wa_graph_version: str = "v23.0"

    # --- LLM ---
    llm_provider: str = "anthropic"  # "anthropic" (Claude API) or "bedrock" (AWS, Mantle client)
    llm_model: str = "claude-opus-5"
    llm_effort: str = "low"  # low effort keeps chat replies fast; raise if evals show headroom
    aws_region: str = "ap-south-1"
    verify_answers: bool = True  # second pass that checks every claim against the sources

    # --- Retrieval ---
    knowledge_dir: Path = Path("knowledge")
    retrieval_top_k: int = 5
    retrieval_min_score: float = 1.0  # below this, we don't even ask the model

    # --- Identity ---
    staff_directory_csv: Path = Path("data/staff_directory.csv")
    staff_session_hours: int = 12
    max_login_attempts: int = 3
    lockout_minutes: int = 30

    # --- Hospital-specific values (must be set before go-live) ---
    hospital_name: str = "VPS Lakeshore"
    emergency_phone: str = "<SET EMERGENCY_PHONE>"
    front_desk_phone: str = "<SET FRONT_DESK_PHONE>"
    privacy_notice_url: str = "<SET PRIVACY_NOTICE_URL>"
    report_pickup_note: str = "<SET REPORT_PICKUP_NOTE, e.g. where/how patients get a copy>"

    # --- HIS ---
    his_adapter: str = "mock"  # "mock" or "elider"
    elider_base_url: str = ""
    elider_api_key: str = ""

    # --- Infra ---
    redis_url: str = ""  # empty = in-memory store (dev/tests only; not safe with >1 worker)
    database_url: str = "sqlite:///data/audit.db"
    audit_hash_secret: str = "change-me"  # HMAC key used to pseudonymise phone numbers in logs
    rate_limit_per_minute: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
