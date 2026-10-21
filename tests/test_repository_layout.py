from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class RepositoryLayoutTests(unittest.TestCase):
    def test_repository_policy_protects_runtime_inputs_and_tracks_only_source(self):
        protected_paths = (
            "inputs/customer-questionnaire.pdf",
            "analysis/local-notes.md",
            "output/report.pdf",
            "data/state.db",
            "secrets/master.key",
            "var/workflow/state.sqlite3-wal",
            "var/results/report.json",
            "results/report.json",
            "server/src/autofill/.env",
            "web/src/.env.local",
            "server/src/autofill/customer.pdf",
            "web/src/customer.pdf",
            "web/public/customer.pdf",
            "web/tests/customer.pdf",
        )
        for relative in protected_paths:
            with self.subTest(path=relative):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", relative],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, f"path should be ignored: {relative}")

        tracked_source_paths = (
            "README.md",
            ".github/workflows/ci.yml",
            "server/src/autofill/application/service.py",
            "web/src/App.vue",
            "web/src/shared/state.js",
            "web/package-lock.json",
            "tests/integration/test_api_workflow_e2e.py",
            "tests/test_repository_layout.py",
        )
        for relative in tracked_source_paths:
            with self.subTest(path=relative):
                result = subprocess.run(
                    ["git", "check-ignore", "--no-index", "-q", relative],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1, f"source must remain trackable: {relative}")

    def test_ci_pins_project_runtimes_and_runs_local_contract_checks(self):
        self.assertEqual((ROOT / ".python-version").read_text(encoding="utf-8").strip(), "3.12.15")
        self.assertEqual((ROOT / ".node-version").read_text(encoding="utf-8").strip(), "24.13.0")
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for required in (
            "permissions:",
            "contents: read",
            "uv sync --project server --locked --all-groups",
            "python -m autofill.adapters.http.export_openapi --check",
            "scripts/test_tracked.py",
            "npm ci --prefix web",
            "npm test --prefix web",
            "npm run build --prefix web",
            "docker compose --env-file /dev/null build",
        ):
            with self.subTest(required=required):
                self.assertIn(required, workflow)

    def test_docker_build_contexts_default_deny_and_reject_local_state(self):
        required_rules = {
            "server/.dockerignore": ("**", "!Dockerfile", "!src/**/*.py"),
            "web/.dockerignore": ("**", "!Dockerfile", "!index.html", "!package-lock.json",
                                   "!src/main.js", "!src/App.vue", "!public/static/document-questionnaire.svg"),
            "deploy/processor/.dockerignore": ("**", "!Dockerfile"),
        }
        for relative, rules in required_rules.items():
            content = (ROOT / relative).read_text(encoding="utf-8").splitlines()
            with self.subTest(context=relative):
                self.assertEqual(content[0], "**")
                for rule in rules:
                    self.assertIn(rule, content)
                for sensitive in ("**/.env", "**/secrets/", "**/var/", "**/*.sqlite*"):
                    if relative.endswith("processor/.dockerignore"):
                        continue
                    self.assertIn(sensitive, content)
                if relative == "web/.dockerignore":
                    self.assertNotIn("**/results/", content)
                    self.assertNotIn("!src/**", content)
                    self.assertNotIn("!public/**", content)

    def test_web_server_and_deploy_have_separate_roots(self):
        required_paths = (
            ROOT / "server" / "src" / "autofill" / "__init__.py",
            ROOT / "web" / "src" / "App.vue",
            ROOT / "web" / "index.html",
            ROOT / "deploy",
        )

        for path in required_paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.exists(), f"missing repository boundary: {path.relative_to(ROOT)}")

    def test_cli_module_loads_from_server_source_root(self):
        env = {**os.environ, "PYTHONPATH": str(ROOT / "server" / "src")}
        result = subprocess.run(
            [sys.executable, "-m", "autofill", "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())

    def test_runtime_images_and_compose_boundaries_are_separate(self):
        for relative in (
            "web/Dockerfile",
            "server/Dockerfile",
            "server/container-entrypoint.sh",
            "deploy/processor/Dockerfile",
        ):
            with self.subTest(path=relative):
                self.assertTrue((ROOT / relative).is_file(), f"missing runtime boundary: {relative}")

        compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
        services = compose["services"]
        self.assertEqual(set(services), {"web", "server", "processor"})
        self.assertEqual(services["web"]["build"]["context"], "./web")
        self.assertEqual(services["server"]["build"]["context"], "./server")
        self.assertEqual(services["processor"]["build"]["context"], "./deploy/processor")
        self.assertEqual(services["processor"]["build"]["additional_contexts"]["server"], "./server")
        self.assertEqual(services["processor"]["network_mode"], "none")
        self.assertIn("ports", services["web"])
        self.assertNotIn("ports", services["server"])
        self.assertNotIn("ports", services["processor"])
        for service in services.values():
            for volume in service.get("volumes", []):
                self.assertTrue(volume.startswith("./var/"), volume)

    def test_web_uses_vue_and_shared_api_feature_boundaries(self):
        required_modules = (
            ROOT / "web" / "src" / "main.js",
            ROOT / "web" / "src" / "App.vue",
            ROOT / "web" / "src" / "shared" / "api.js",
            ROOT / "web" / "src" / "shared" / "state.js",
            ROOT / "web" / "src" / "features" / "configuration" / "configuration.js",
            ROOT / "web" / "src" / "features" / "files" / "files.js",
            ROOT / "web" / "src" / "features" / "jobs" / "jobs.js",
            ROOT / "web" / "src" / "components" / "ResultPanel.vue",
            ROOT / "web" / "nginx.conf",
        )
        for path in required_modules:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertTrue(path.is_file(), f"missing web boundary: {path.relative_to(ROOT)}")

        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('src="/src/main.js"', html)
        self.assertNotIn("AUTOFILL_TOKEN", html)
        package = (ROOT / "web" / "package.json").read_text(encoding="utf-8")
        self.assertIn('"vue"', package)
        self.assertIn('"pinia"', package)
        self.assertFalse((ROOT / "server" / "src" / "autofill" / "static").exists())
        self.assertFalse((ROOT / "server" / "src" / "autofill" / "web.html").exists())


if __name__ == "__main__":
    unittest.main()
