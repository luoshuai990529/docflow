from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class DomainContractTests(unittest.TestCase):
    def load_domain_module(self, module_name: str):
        try:
            spec = importlib.util.find_spec(module_name)
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            self.fail(f"missing domain module {module_name}")
        return importlib.import_module(module_name)

    def test_domain_contract_import_does_not_pull_in_adapters_or_io(self):
        script = """
import builtins
blocked_roots = {'fastapi', 'sqlite3', 'urllib', 'http', 'pathlib', 'json',
                 'openpyxl', 'pymupdf', 'pypdf', 'cryptography', 'PIL'}
real_import = builtins.__import__
def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name.split('.', 1)[0] in blocked_roots or name.startswith('autofill.adapters'):
        raise AssertionError(f'domain imported forbidden dependency: {name}')
    return real_import(name, globals, locals, fromlist, level)
builtins.__import__ = guarded_import
from autofill.domain.models import Decision, Evidence, Question, SourceSpan, Target, Verification, WriteResult
from autofill.domain.decisions import validate_decisions
from autofill.domain.errors import normalize_error_code
from autofill.domain.publication import can_publish_candidate, workflow_outcome
"""
        env = {**os.environ, "PYTHONPATH": str(ROOT / "server" / "src")}
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_domain_types_are_shared_and_keep_bundle_wire_shape(self):
        models = self.load_domain_module("autofill.domain.models")
        legacy = importlib.import_module("autofill.models")

        self.assertIs(legacy.Question, models.Question)
        self.assertIs(legacy.Decision, models.Decision)
        self.assertNotIn("dump_json", vars(models))
        evidence = models.Evidence("span-1", "Acme Ltd")
        decision = models.Decision(
            "company", "ready", "Acme Ltd", [], ["span-1"], "Acme Ltd", "explicit", [evidence]
        )
        self.assertEqual(
            importlib.import_module("dataclasses").asdict(decision)["evidence"],
            [{"span_id": "span-1", "quote": "Acme Ltd"}],
        )

    def test_decision_rules_validate_choice_evidence_and_exact_unit_conversion(self):
        models = self.load_domain_module("autofill.domain.models")
        decisions = self.load_domain_module("autofill.domain.decisions")
        question = models.Question("q1", "Company", "text", [], ["t1"])
        span = models.SourceSpan("s1", {}, "Company: Acme Ltd", "source-hash")
        valid = models.Decision(
            "q1", "ready", "Acme Ltd", [], ["s1"], "Acme Ltd", "explicit",
            [models.Evidence("s1", "Acme Ltd")],
        )

        result = decisions.validate_decisions([valid], [question], [span], "real")

        self.assertEqual(result[0].value, "Acme Ltd")
        self.assertTrue(decisions.supported_conversion("1000 mg", "1 g"))
        self.assertFalse(decisions.supported_conversion("1000 mg", "1 kg"))
        invalid_choice = models.Decision("q1", "ready", None, ["NOT PRINTED"], ["s1"], "Acme Ltd", "explicit")
        with self.assertRaisesRegex(ValueError, "CHOICE_VALUE"):
            decisions.validate_decisions([invalid_choice], [models.Question("q1", "Choose", "single_choice", ["Yes"], ["t1"])], [span], "real")
        invalid_evidence = models.Decision(
            "q1", "ready", "Acme Ltd", [], ["missing-span"], "Acme Ltd", "explicit",
            [models.Evidence("missing-span", "Acme Ltd")],
        )
        with self.assertRaisesRegex(ValueError, "EVIDENCE_ID"):
            decisions.validate_decisions([invalid_evidence], [question], [span], "real")

    def test_publication_policy_withholds_failed_or_unverified_candidates(self):
        models = self.load_domain_module("autofill.domain.models")
        publication = self.load_domain_module("autofill.domain.publication")
        written = [models.WriteResult("q1", "t1", "written", "Acme", "Acme", "")]
        verified = models.Verification(True, "passed", False, {})
        blocked = models.Verification(True, "needs_review", True, {})
        failed = models.Verification(False, "needs_review", True, {})

        self.assertTrue(publication.can_publish_candidate(verified, written))
        self.assertFalse(publication.can_publish_candidate(blocked, written))
        self.assertFalse(publication.can_publish_candidate(verified, []))
        self.assertFalse(publication.can_publish_candidate(failed, written))
        self.assertTrue(publication.can_publish_general_candidate(verified, written))
        self.assertFalse(publication.can_publish_general_candidate(models.Verification(True, "not_run", True, {}), written))
        self.assertEqual(publication.workflow_outcome(verified, [{"status": "written"}]), "complete")
        self.assertEqual(publication.workflow_outcome(blocked, [{"status": "candidate_not_delivered"}]), "partial")
        self.assertEqual(publication.workflow_outcome(failed, [{"status": "written"}]), "failed")
        self.assertEqual(publication.general_workflow_outcome(verified, None, [], [{"status": "candidate_not_delivered"}]), "partial")

    def test_error_code_normalization_stays_pure_and_redacts_unrecognized_values(self):
        errors = self.load_domain_module("autofill.domain.errors")

        self.assertEqual(errors.normalize_error_code(ValueError("QUOTE_MISMATCH: details")), "QUOTE_MISMATCH")
        self.assertEqual(errors.normalize_error_code(ValueError("private text\nwith details")), "PROCESSING_ERROR")


if __name__ == "__main__":
    unittest.main()
