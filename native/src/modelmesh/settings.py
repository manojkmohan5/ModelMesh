"""Application configuration. Credentials stay on the trusted API/worker hosts."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import shutil
import tempfile
from functools import cached_property
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", populate_by_name=True, hide_input_in_errors=True,
    )

    data_dir: Path = Field(default=Path(".data"), validation_alias="DATA_DIR")
    # A SQLite URL; empty means modelmesh.db in DATA_DIR.
    database_url: str = Field(default="", validation_alias="MODELMESH_DATABASE_URL")
    encryption_key: SecretStr = Field(default=SecretStr(""), validation_alias="ENCRYPTION_KEY")
    workspace_password: SecretStr = Field(default=SecretStr(""), validation_alias="WORKSPACE_PASSWORD")
    administrator: str = Field(default="admin", validation_alias="MODELMESH_ADMIN_USER")
    gateway_url: str = Field(default="", validation_alias="LITELLM_API_BASE")
    gateway_key: SecretStr = Field(default=SecretStr(""), validation_alias="LITELLM_API_KEY")
    default_model: str = Field(
        default="openai/gpt-6-astra", validation_alias=AliasChoices("MODELMESH_MODEL", "AGENT_MODEL")
    )
    model_catalog: str = Field(default="", validation_alias="MODELMESH_MODELS")
    # The model that reads each "auto" message and picks the model to run it. Defaults to default_model.
    router_model: str = Field(default="", validation_alias="MODELMESH_ROUTER_MODEL")
    # Laya task-type classifier (laya/server.py). Without it, Auto uses the LLM router alone.
    laya_url: str = Field(default="", validation_alias="MODELMESH_LAYA_URL")
    laya_key: SecretStr = Field(default=SecretStr(""), validation_alias="LAYA_API_KEY")
    # Models whose provider cannot enforce strict tool schemas for the agent's tool set (comma-separated ids).
    # Lumen's beam-1.0 refuses them: "cannot enforce strict tools with automatic tool choice".
    non_strict_tool_models: str = Field(default="", validation_alias="MODELMESH_NON_STRICT_TOOL_MODELS")
    temporal_address: str = Field(default="127.0.0.1:7233", validation_alias="TEMPORAL_ADDRESS")
    temporal_namespace: str = Field(default="default", validation_alias="TEMPORAL_NAMESPACE")
    temporal_queue: str = Field(default="modelmesh-v1", validation_alias="TEMPORAL_TASK_QUEUE")
    worker_deployment: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,127})?$",
        validation_alias="MODELMESH_WORKER_DEPLOYMENT",
    )
    worker_build_id: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,127})?$",
        validation_alias="MODELMESH_WORKER_BUILD_ID",
    )
    temporal_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="TEMPORAL_API_KEY")
    temporal_tls: bool = Field(default=False, validation_alias="TEMPORAL_TLS")
    compress_temporal_payloads: bool = Field(
        default=False, validation_alias="MODELMESH_COMPRESS_TEMPORAL_PAYLOADS",
    )
    history_continue_bytes: int = Field(
        default=8 * 1024 * 1024, ge=16384, le=20 * 1024 * 1024,
        validation_alias="MODELMESH_HISTORY_CONTINUE_BYTES",
    )
    history_continue_events: int = Field(
        default=8000, ge=100, le=10000, validation_alias="MODELMESH_HISTORY_CONTINUE_EVENTS",
    )
    worker_shutdown_seconds: int = Field(
        default=0, ge=0, le=240, validation_alias="MODELMESH_WORKER_SHUTDOWN_SECONDS",
    )
    temporal_worker_activities: int = Field(default=120, ge=1, le=10000, validation_alias="TEMPORAL_WORKER_ACTIVITIES")
    temporal_workflow_cache_size: int = Field(default=200, ge=0, le=100000, validation_alias="TEMPORAL_WORKFLOW_CACHE_SIZE")
    temporal_dispatch_concurrency: int = Field(default=10, ge=1, le=1000, validation_alias="TEMPORAL_DISPATCH_CONCURRENCY")
    temporal_dispatch_batch_size: int = Field(default=200, ge=1, le=10000, validation_alias="TEMPORAL_DISPATCH_BATCH_SIZE")
    # The image of the agent's computer, one container per conversation (workspace.Dockerfile).
    docker_image: str = Field(default="modelmesh-workspace", validation_alias="MODELMESH_DOCKER_IMAGE")
    # Replay-only: new turns and delegates have no model-request-count cap.
    model_request_limit: int = Field(default=100, ge=1, le=1000, validation_alias="MODELMESH_MODEL_CALL_LIMIT")
    model_timeout_seconds: int = Field(default=300, ge=10, le=900, validation_alias="MODELMESH_MODEL_TIMEOUT")
    optional_question_timeout_seconds: int = Field(
        default=90, ge=5, le=900, validation_alias="MODELMESH_OPTIONAL_QUESTION_TIMEOUT",
    )
    secure_cookies: bool = Field(default=False, validation_alias="MODELMESH_SECURE_COOKIES")
    public_url: str = Field(default="", validation_alias=AliasChoices("MODELMESH_PUBLIC_URL", "PUBLIC_URL"))

    @model_validator(mode="after")
    def worker_version_is_complete(self) -> Settings:
        if bool(self.worker_deployment) != bool(self.worker_build_id):
            raise ValueError("Worker deployment name and build ID must be configured together.")
        return self

    @property
    def models(self) -> list[str]:
        return list(dict.fromkeys([
            self.default_model,
            *(value.strip() for value in self.model_catalog.split(",") if value.strip()),
        ]))

    @property
    def routing_enabled(self) -> bool:
        """Smart routing needs several models and a gateway this process can call."""
        return (
            len(self.models) > 1 and bool(self.gateway_url.strip()) and bool(self.gateway_key.get_secret_value())
        )

    @property
    def sqlalchemy_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{self.data_dir.resolve() / 'modelmesh.db'}"

    @cached_property
    def master_key(self) -> bytes:
        """Use an explicit key in production, or one private local development key."""
        supplied = self.encryption_key.get_secret_value()
        if supplied:
            if len(supplied) < 32:
                raise ValueError("ENCRYPTION_KEY must contain at least 32 characters.")
            return base64.urlsafe_b64encode(hashlib.sha256(supplied.encode()).digest())
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.data_dir / ".master-key"
        if path.is_file():
            value = path.read_bytes()
        else:
            value = base64.urlsafe_b64encode(secrets.token_bytes(32))
            descriptor, temporary = tempfile.mkstemp(prefix=".key-", dir=self.data_dir)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(value)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    value = path.read_bytes()
            finally:
                Path(temporary).unlink(missing_ok=True)
        if len(base64.urlsafe_b64decode(value)) != 32:
            raise ValueError("Invalid local master key.")
        return value

    def validate_worker(self) -> None:
        if shutil.which("docker") is None:
            raise ValueError("The worker needs the docker CLI and the host Docker socket to start the agent's computer.")
        if not self.gateway_url.strip():
            raise ValueError("Set LITELLM_API_BASE before starting an execution worker.")
        if not self.gateway_key.get_secret_value():
            raise ValueError("Set LITELLM_API_KEY before starting an execution worker.")
        origin = urlsplit(self.gateway_url)
        if origin.username or origin.password or not (
            origin.scheme == "https"
            or (origin.scheme == "http" and origin.hostname in {"localhost", "127.0.0.1", "::1"})
        ):
            raise ValueError("The model gateway must use HTTPS or an explicit loopback test endpoint.")

