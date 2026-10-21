"""HTTP-only request and response DTOs."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ApiError(BaseModel):
    code: str
    message: str
    diagnostics: dict[str, Any] | None = None


class ApiErrorEnvelope(BaseModel):
    error: ApiError


class SessionBootstrap(BaseModel):
    csrf_token: str


class ConfigMetadata(BaseModel):
    id: str
    name: str
    provider: str
    model: str
    base_url: str
    output_mode: str
    protocol: str
    has_key: bool
    key_mask: str
    updated_at: str


class ConfigList(BaseModel):
    configs: list[ConfigMetadata]
    configured: bool


class ConfigWrite(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    provider: str = Field(min_length=1, max_length=80)
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)
    output_mode: str | None = Field(default=None, max_length=32)
    protocol: str | None = Field(default=None, max_length=80)
    api_key: SecretStr | None = None
    clear_key: bool = False

    def to_store_body(self) -> dict[str, Any]:
        body = self.model_dump(exclude={'api_key'}, exclude_none=True)
        if self.api_key is not None:
            body['api_key'] = self.api_key.get_secret_value()
        return body


class ConfigResult(BaseModel):
    config: ConfigMetadata


class DeleteResult(BaseModel):
    deleted: bool


class UploadReceipt(BaseModel):
    token: str
    name: str
    size: int


class JobSubmit(StrictRequest):
    request_id: str = Field(min_length=16, max_length=80, pattern=r'^[A-Za-z0-9_-]+$')
    config_id: str = Field(min_length=1, max_length=80)
    questionnaire: str = Field(pattern=r'^[a-f0-9]{32}$')
    sources: list[str] = Field(max_length=20)
    answer_mode: Literal['grounded', 'autonomous'] = 'grounded'
    decision_config_id: str | None = Field(default=None, max_length=80)


class JobAccepted(BaseModel):
    id: str


class CancelAccepted(BaseModel):
    accepted: bool


class WorkflowNodeView(BaseModel):
    node_id: str
    status: Literal['pending', 'running', 'success', 'skipped', 'failed', 'interrupted']
    occurred_at: str | None = None
    phase: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    error_code: str | None = None


class LegacyNodeView(BaseModel):
    stage: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    phase: str | None = None
    completed: int | None = None
    total: int | None = None
    batch: int | None = None
    batches: int | None = None


class JobView(BaseModel):
    """Stable public job fields with additive compatibility data retained."""

    model_config = ConfigDict(extra='allow')

    id: str
    status: str
    created_at: str
    updated_at: str
    finished_at: str | None = None
    files: list[str] = Field(default_factory=list)
    stage: str | None = None
    nodes: list[LegacyNodeView] = Field(default_factory=list)
    workflow_nodes: list[WorkflowNodeView] | None = None
    progress_log: list[dict[str, Any]] = Field(default_factory=list)
    error: ApiError | None = None
    summary: dict[str, Any] | None = None
    counts: dict[str, int] | None = None
    cancelling: bool | None = None
    review: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    decision_config: dict[str, Any] | None = None
    answer_mode: str | None = None
    questionnaire_name: str | None = None
    source_name: str | None = None


class JobList(BaseModel):
    jobs: list[JobView]


class ReportView(BaseModel):
    """The report remains an additive machine-readable document contract."""

    model_config = ConfigDict(extra='allow')

    result_summary: dict[str, Any] | None = None
    processing_warnings: list[str] = Field(default_factory=list)
    published_file: str | None = None
