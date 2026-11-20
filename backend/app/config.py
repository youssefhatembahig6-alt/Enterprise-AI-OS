"""Application settings loaded from environment variables.

Secrets are ``SecretStr`` so they never appear in ``repr`` output or logs.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PLACEHOLDER_MARKERS = ("change-me", "changeme", "example")


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    environment: Environment = Field(default=Environment.DEVELOPMENT, alias="EAIOS_ENV")

    # Application database role (non-owner, NOBYPASSRLS). Never the migration role.
    database_url: SecretStr = Field(alias="DATABASE_URL")
    database_pool_size: int = Field(default=5, alias="DATABASE_POOL_SIZE", ge=1, le=50)

    redis_url: SecretStr = Field(alias="REDIS_URL")
    qdrant_url: str = Field(alias="QDRANT_URL")
    s3_endpoint_url: str = Field(alias="S3_ENDPOINT_URL")

    # External model gateway (Colab). Optional in M0: no model capability is claimed.
    model_gateway_url: str | None = Field(default=None, alias="LLM_BASE_URL")

    jwt_secret: SecretStr = Field(alias="JWT_SECRET")
    jwt_issuer: str = Field(default="eaios-api", alias="JWT_ISSUER")
    jwt_audience: str = Field(default="eaios-web", alias="JWT_AUDIENCE")
    access_token_ttl_seconds: int = Field(
        default=900, alias="ACCESS_TOKEN_TTL_SECONDS", ge=60, le=3600
    )

    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"],
        alias="CORS_ALLOWED_ORIGINS",
    )

    login_max_failures_per_identity: int = Field(
        default=5, alias="LOGIN_MAX_FAILURES_PER_IDENTITY", ge=1
    )
    login_max_failures_per_client: int = Field(
        default=20, alias="LOGIN_MAX_FAILURES_PER_CLIENT", ge=1
    )
    login_failure_window_seconds: int = Field(
        default=900, alias="LOGIN_FAILURE_WINDOW_SECONDS", ge=1
    )
    # Argon2id verification costs ~64 MiB each. Measured in the 384 MiB API
    # container: ~68 MiB baseline + 64 MiB per concurrent verification, so 2
    # slots peak near 196 MiB. Applies per API process (uvicorn runs one).
    password_verify_concurrency: int = Field(
        default=2, alias="PASSWORD_VERIFY_CONCURRENCY", ge=1, le=16
    )
    password_verify_queue_timeout_seconds: float = Field(
        default=5.0, alias="PASSWORD_VERIFY_QUEUE_TIMEOUT_SECONDS", gt=0, le=30
    )
    rate_limit_key_prefix: str = Field(default="eaios:ratelimit", alias="RATE_LIMIT_KEY_PREFIX")

    readiness_timeout_seconds: float = Field(
        default=2.0, alias="READINESS_TIMEOUT_SECONDS", gt=0, le=10
    )

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _no_wildcard(cls, value: list[str]) -> list[str]:
        if any(origin == "*" for origin in value):
            raise ValueError("CORS origins must be listed explicitly; '*' is not allowed")
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _secret_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 bytes")
        return value

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.environment is Environment.PRODUCTION:
            secret = self.jwt_secret.get_secret_value().lower()
            if any(marker in secret for marker in _PLACEHOLDER_MARKERS):
                raise ValueError("JWT_SECRET looks like a placeholder; refusing production start")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
