"""Immutable, validated instructions for creating one candidate document."""

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .models import Decision, DocumentData


TEST_LABEL = 'FICTIONAL DATA — LAYOUT TEST ONLY'


def _freeze(value):
    """Copy JSON-like target metadata into read-only mappings and tuples."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return deepcopy(value)


@dataclass(frozen=True)
class WriteOperation:
    question_id: str
    target_id: str
    value: str
    locator: Mapping[str, object]
    constraints: Mapping[str, object]

    @property
    def id(self):
        return self.target_id


@dataclass(frozen=True)
class WritePlan:
    source_sha256: str
    questionnaire_kind: str
    mode: str
    operations: tuple[WriteOperation, ...]
    questionnaire_metadata: Mapping[str, object]
    target_count: int
    test_label_target_id: str | None = None


def target_value(target, decision):
    """Resolve a validated decision into the literal value for one target."""
    if target.constraints.get('content_type') == 'checkbox':
        option = target.locator.get('option_id') or target.constraints.get('checked_option')
        return 'checked' if option in decision.option_ids else 'unchecked'
    if decision.value is not None:
        return decision.value
    if target.constraints.get('options_text'):
        return '、'.join(decision.option_ids)
    if len(decision.option_ids) != 1:
        raise ValueError('UNSUPPORTED_MULTI_VALUE_TARGET')
    return target.constraints.get('exports', {}).get(decision.option_ids[0], decision.option_ids[0])


def build_write_plan(questionnaire: DocumentData, decisions: list[Decision], mode: str) -> WritePlan:
    """Validate decision-to-target references and freeze the exact write set."""
    if mode not in ('real', 'fictional_test'):
        raise ValueError('MODE')

    questions = {}
    for question in questionnaire.questions:
        if question.id in questions:
            raise ValueError('DUPLICATE_QUESTION_ID')
        questions[question.id] = question

    targets = {}
    for target in questionnaire.targets:
        if target.id in targets:
            raise ValueError('DUPLICATE_TARGET_ID')
        targets[target.id] = target

    by_question = {}
    for decision in decisions:
        if decision.status != 'ready':
            continue
        if decision.question_id not in questions:
            raise ValueError('UNKNOWN_QUESTION')
        if decision.question_id in by_question:
            raise ValueError('DUPLICATE_DECISION')
        by_question[decision.question_id] = decision

    for question in questionnaire.questions:
        if len(question.target_ids) != len(set(question.target_ids)):
            raise ValueError('DUPLICATE_QUESTION_TARGET')
        if any(target_id not in targets for target_id in question.target_ids):
            raise ValueError('UNKNOWN_TARGET')

    labels = [target for target in questionnaire.targets if target.constraints.get('test_label')]
    if mode == 'fictional_test' and len(labels) != 1:
        raise ValueError('TEST_LABEL_TARGET_REQUIRED')
    label_id = labels[0].id if mode == 'fictional_test' else None
    if label_id and not any(label_id in question.target_ids and question.id in by_question
                            for question in questionnaire.questions):
        raise ValueError('TEST_LABEL_NOT_READY')

    operations = []
    for question in questionnaire.questions:
        decision = by_question.get(question.id)
        if decision is None:
            continue
        for target_id in question.target_ids:
            target = targets[target_id]
            value = TEST_LABEL if mode == 'fictional_test' and target_id == label_id else target_value(target, decision)
            if not isinstance(value, str):
                raise ValueError('WRITE_VALUE_REQUIRED')
            operations.append(WriteOperation(
                question.id, target.id, value, _freeze(target.locator), _freeze(target.constraints),
            ))

    return WritePlan(
        questionnaire.sha256,
        questionnaire.kind,
        mode,
        tuple(operations),
        _freeze(questionnaire.metadata),
        len(questionnaire.targets),
        label_id,
    )
