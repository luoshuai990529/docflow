"""Framework-free values shared by the fixed document workflow."""

from dataclasses import dataclass, field


@dataclass
class Question:
    id: str
    text: str
    kind: str
    options: list[str]
    target_ids: list[str]


@dataclass
class SourceSpan:
    id: str
    locator: dict
    text: str
    source_sha256: str


@dataclass
class Target:
    id: str
    locator: dict
    constraints: dict


@dataclass
class DocumentData:
    kind: str
    sha256: str
    spans: list[SourceSpan]
    questions: list[Question]
    targets: list[Target]
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    span_id: str
    quote: str


@dataclass
class Decision:
    question_id: str
    status: str
    value: str | None
    option_ids: list[str]
    span_ids: list[str]
    quote: str | None
    reason: str
    # Dict entries are accepted at the legacy JSON boundary; domain callers
    # can use Evidence without changing the existing serialized bundle shape.
    evidence: list[Evidence | dict] = field(default_factory=list)


@dataclass
class WriteResult:
    question_id: str
    target_id: str
    status: str
    expected_value: str | None
    readback_value: str | None
    reason: str


@dataclass
class Verification:
    content_ok: bool
    layout_check: str
    needs_review: bool
    details: dict
