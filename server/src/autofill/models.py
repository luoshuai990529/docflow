"""Workflow serialization and result models shared by CLI and adapters."""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .domain.models import (
    Decision,
    DocumentData,
    Evidence,
    Question,
    SourceSpan,
    Target,
    Verification,
    WriteResult,
)


@dataclass
class RunResult:
    output_path: Path | None
    report_json: Path
    report_markdown: Path
    outcome: str


def dump_json(path: Path, value):
    payload = asdict(value) if hasattr(value, "__dataclass_fields__") else value
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def document_from_dict(value: dict) -> DocumentData:
    return DocumentData(
        value["kind"],
        value["sha256"],
        [SourceSpan(**span) for span in value["spans"]],
        [Question(**question) for question in value["questions"]],
        [Target(**target) for target in value["targets"]],
        value.get("metadata", {}),
    )


__all__ = [
    "Decision",
    "DocumentData",
    "Evidence",
    "Question",
    "RunResult",
    "SourceSpan",
    "Target",
    "Verification",
    "WriteResult",
    "document_from_dict",
    "dump_json",
]
