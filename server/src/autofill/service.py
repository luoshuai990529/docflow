"""Production composition root for the local workflow application."""

from pathlib import Path

from .adapters.models.openai_chat import OpenAIChatClient
from .adapters.persistence.sqlite_store import SQLiteWorkflowStore
from .adapters.documents.catalog import resolve_questionnaire
from .adapters.documents.preflight import LocalDocumentPreflight
from .adapters.processor.processor import Processor
from .application.service import (
    INLINE_CONFIG_FIELDS,
    MAX_FILE,
    UPLOAD_COPIES,
    UPLOAD_NAMES,
    WorkflowApplicationService,
)
from .adapters.models.jev_client import JevClient


class WorkflowService(WorkflowApplicationService):
    """Wire production adapters and provide the application service."""

    def __init__(self, root, client_factory=None, questionnaire_resolver=None, state_dir=None, key_file=None,
                 decision_client_factory=None, store=None, processor_factory=None, preflight=None):
        root = Path(root).resolve()
        state = Path(state_dir) if state_dir else root / '.state'
        key_path = Path(key_file) if key_file else root / '.keys/master.key'
        store = store if store is not None else SQLiteWorkflowStore(state, key_path)
        model_factory = client_factory if client_factory is not None else (lambda config, key: OpenAIChatClient(config, api_key=key))
        decision_factory = decision_client_factory if decision_client_factory is not None else (lambda config, key: JevClient(config, api_key=key))
        super().__init__(root, store=store, client_factory=model_factory,
                         processor_factory=processor_factory if processor_factory is not None else (lambda **kwargs: Processor(**kwargs)),
                         questionnaire_resolver=questionnaire_resolver if questionnaire_resolver is not None else resolve_questionnaire,
                         preflight=preflight if preflight is not None else LocalDocumentPreflight(),
                         decision_client_factory=decision_factory)
