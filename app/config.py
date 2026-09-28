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

    # --- Handoff dashboard (/desk) ---
    public_base_url: str = "http://localhost:8000"  # used to build sign-in links sent on WhatsApp
    desk_session_hours: int = 12
    desk_cookie_secure: bool = True  # set false only for local http testing
    desk_allowed_cidrs: str = ""  # e.g. "10.0.0.0/8,203.0.113.4/32"; empty = any network

    # --- Emergency alerts to duty staff ---
    # Tiers are separated by ";" and numbers within a tier by ",". Tier 1 is alerted as soon as a
    # ticket opens; each further tier after another ALERT_ESCALATE_MINUTES with nobody taking it.
    # e.g. "919000000001,919000000002;919000000010;919000000020"
    alert_tiers: str = ""  # fixed last-resort tiers; the duty roster (DUTY_ROSTER_CSV / desk upload) comes first
    duty_roster_csv: Path = Path("data/duty_roster.csv")  # loaded once as the first version if no roster uploaded yet
    alert_escalate_minutes: int = 3
    alert_reasons: str = "emergency"  # ticket reasons that page people, e.g. "emergency,clinical"
    alert_template: str = ""  # name of the Meta-approved WhatsApp template (see README)
    alert_template_lang: str = "en"
    alert_include_preview: bool = True  # include a redacted snippet of the message in the alert
    alert_webhook_url: str = ""  # optional: also POST each alert here (phone-call / paging bridge)
    alert_webhook_secret: str = ""  # HMAC key for the X-Lakeshore-Signature header
    alert_check_seconds: int = 30  # how often the escalation check runs

    # --- Infra ---
    redis_url: str = ""  # empty = in-memory store (dev/tests only; not safe with >1 worker)
    database_url: str = "sqlite:///data/audit.db"
    audit_hash_secret: str = "change-me"  # HMAC key used to pseudonymise phone numbers in logs
    rate_limit_per_minute: int = 12


@lru_cache
def get_settings() -> Settings:
    return Settings()
