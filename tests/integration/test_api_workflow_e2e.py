"""End-to-end API checks using synthetic documents and a loopback-only fake model."""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pymupdf as M

from autofill.adapters.http.app import create_app
from autofill.adapters.models.openai_chat import OpenAIChatClient
from autofill.service import WorkflowService


ORIGIN = "http://127.0.0.1:8765"
API = "/api/v1"


class FakeModel:
    def __init__(self, *, block_phase: str | None = None):
        self.phases: list[str] = []
        self.block_phase = block_phase
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set() if block_phase is None else None
        context = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                content = request["messages"][1]["content"]
                payload = json.loads(content.split("\n", 1)[1])
                phase = payload["phase"]
                context.phases.append(phase)
                if phase == context.block_phase:
                    context.started.set()
                    context.release.wait(5)
                result = context.response(phase, payload)
                body = json.dumps({"choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(result)},
                }]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    @staticmethod
    def response(phase: str, payload: dict) -> dict:
        if phase == "question_inventory":
            spans = {item["id"]: item for item in payload["spans"]}
            return {
                "reviewed_span_ids": list(spans),
                "items": [{
                    "span_ids": slot["span_ids"],
                    "quote": spans[slot["span_ids"][0]]["text"],
                    "kind": "text",
                    "options": [],
                    "target_id": slot["target_id"],
                } for slot in payload["slots"]],
            }
        if phase == "answer_support_review":
            reviews = []
            for item in payload["questions"]:
                supported = (
                    item["answer"]["value"] == "SYNTHETIC CO"
                    and any("Company: SYNTHETIC CO" in span["text"]
                            for span in payload["source_spans"])
                )
                reviews.append({
                    "question_id": item["question"]["id"],
                    "verdict": "supported" if supported else "unsupported",
                    "transformations": [],
                    "reason": "Synthetic source supports the answer.",
                    "calculations": [],
                })
            return {"reviews": reviews}
        if phase == "autonomous_context":
            return {"scenario": "Synthetic local example.", "assumptions": "Fictional test data."}
        if phase in ("autonomous_answers", "answer_compaction"):
            return {"answers": [{
                "question_id": question["id"],
                "value": "DEMO COMPANY",
                "option_ids": [],
                "assumptions": "Fictional test data.",
                "reason": "Synthetic local answer.",
            } for question in payload["questions"]]}

        span = payload["source_spans"][0]
        return {"decisions": [{
            "question_id": question["id"],
            "status": "ready",
            "value": "SYNTHETIC CO",
            "option_ids": [],
            "span_ids": [span["id"]],
            "quote": "SYNTHETIC CO",
            "reason": "Synthetic source supports this value.",
        } for question in payload["questions"]]}

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class VersionedApi:
    def __init__(self, app):
        self.runner = asyncio.Runner()
        self.lifespan = app.router.lifespan_context(app)
        self.runner.run(self.lifespan.__aenter__())
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 41000)),
            base_url=ORIGIN,
        )

    def request(self, method: str, path: str, **kwargs):
        return self.runner.run(self.client.request(method, path, **kwargs))

    def close(self):
        self.runner.run(self.client.aclose())
        self.runner.run(self.lifespan.__aexit__(None, None, None))
        self.runner.close()


class WorkflowApiEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.questionnaire = self.root / "synthetic-questionnaire.pdf"
        with M.open() as document:
            page = document.new_page(width=600, height=300)
            page.insert_text((30, 60), "Company name")
            widget = M.Widget()
            widget.field_name = "Company name"
            widget.field_type = M.PDF_WIDGET_TYPE_TEXT
            widget.rect = M.Rect(30, 80, 300, 110)
            page.add_widget(widget)
            document.save(self.questionnaire)
        self.original_bytes = self.questionnaire.read_bytes()

    def tearDown(self):
        self.temporary.cleanup()

    def make_api(self, model: FakeModel):
        service = WorkflowService(
            self.root / "runtime",
            client_factory=lambda config, key: OpenAIChatClient(config, api_key=key),
        )
        app = create_app(
            service=service,
            public_origin=ORIGIN,
            trusted_proxy_cidrs=("127.0.0.1/32",),
        )
        client = VersionedApi(app)
        session = client.request("GET", f"{API}/session")
        self.assertEqual(session.status_code, 200, session.text)
        headers = {
            "Origin": ORIGIN,
            "X-CSRF-Token": session.json()["csrf_token"],
        }
        config = client.request("POST", f"{API}/configs", headers=headers, json={
            "name": "Synthetic local model",
            "provider": "deepseek",
            "model": "fake-local",
            "base_url": model.base_url,
            "api_key": "FAKE-E2E-KEY",
        })
        self.assertEqual(config.status_code, 201, config.text)
        return service, client, headers, config.json()["config"]["id"]

    def upload(self, client: VersionedApi, headers: dict, path: Path, role: str):
        response = client.request(
            "PUT", f"{API}/uploads",
            params={"name": path.name, "role": role},
            headers=headers | {"Content-Type": "application/octet-stream"},
            content=path.read_bytes(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["token"]

    def await_job(self, client: VersionedApi, identity: str):
        for _ in range(300):
            response = client.request("GET", f"{API}/jobs/{identity}")
            self.assertEqual(response.status_code, 200, response.text)
            job = response.json()
            if job["status"] != "running":
                return job
            time.sleep(0.02)
        self.fail("synthetic workflow did not finish within six seconds")

    def test_v1_runs_grounded_and_autonomous_jobs_through_local_fake_http(self):
        model = FakeModel()
        service, client, headers, config_id = self.make_api(model)
        try:
            source = self.root / "synthetic-source.txt"
            source.write_text("Company: SYNTHETIC CO", encoding="utf-8")

            for mode in ("grounded", "autonomous"):
                with self.subTest(mode=mode):
                    questionnaire_token = self.upload(client, headers, self.questionnaire, "questionnaire")
                    body = {
                        "request_id": f"e2e-{mode}-request-0001",
                        "config_id": config_id,
                        "questionnaire": questionnaire_token,
                        "sources": ([self.upload(client, headers, source, "source")]
                                    if mode == "grounded" else []),
                        "answer_mode": mode,
                    }
                    submitted = client.request("POST", f"{API}/jobs", headers=headers, json=body)
                    self.assertEqual(submitted.status_code, 202, submitted.text)
                    identity = submitted.json()["id"]
                    repeated = client.request("POST", f"{API}/jobs", headers=headers, json=body)
                    self.assertEqual(repeated.status_code, 202, repeated.text)
                    self.assertEqual(repeated.json()["id"], identity)

                    job = self.await_job(client, identity)
                    self.assertEqual(job["status"], "done", job)
                    self.assertEqual(job["answer_mode"], mode)
                    self.assertEqual([node["node_id"] for node in job["workflow_nodes"]], [
                        "preflight", "extract", "understand", "answer", "validate", "write", "verify",
                    ])
                    self.assertTrue(all(node["status"] == "success" for node in job["workflow_nodes"]))
                    self.assertTrue(any(event.get("phase") for event in job["progress_log"]))
                    self.assertIn("filled.pdf", job["files"])

                    report = client.request("GET", f"{API}/jobs/{identity}/report.json")
                    self.assertEqual(report.status_code, 200, report.text)
                    self.assertEqual(report.json()["answer_mode"], mode)
                    download = client.request("GET", f"{API}/jobs/{identity}/download/filled.pdf")
                    self.assertEqual(download.status_code, 200, download.text)
                    self.assertTrue(download.content.startswith(b"%PDF"))
                    self.assertEqual(download.headers["cache-control"], "no-store")
                    if mode == "autonomous":
                        self.assertIn("DEMO COMPANY", report.text)

            self.assertIn("question_inventory", model.phases)
            self.assertIn("answer_support_review", model.phases)
            self.assertIn("autonomous_context", model.phases)
            self.assertIn("autonomous_answers", model.phases)
            self.assertEqual(self.questionnaire.read_bytes(), self.original_bytes)
            self.assertNotIn("FAKE-E2E-KEY", json.dumps(client.request("GET", f"{API}/jobs").json()))
        finally:
            client.close()
            service.close()
            model.close()

    def test_v1_cancellation_withholds_output_after_releasing_local_fake(self):
        model = FakeModel(block_phase="question_inventory")
        service, client, headers, config_id = self.make_api(model)
        try:
            source = self.root / "synthetic-source.txt"
            source.write_text("Company: SYNTHETIC CO", encoding="utf-8")
            body = {
                "request_id": "e2e-cancel-request-0001",
                "config_id": config_id,
                "questionnaire": self.upload(client, headers, self.questionnaire, "questionnaire"),
                "sources": [self.upload(client, headers, source, "source")],
            }
            submitted = client.request("POST", f"{API}/jobs", headers=headers, json=body)
            self.assertEqual(submitted.status_code, 202, submitted.text)
            identity = submitted.json()["id"]
            self.assertTrue(model.started.wait(5), "fake model request was not reached")

            cancelled = client.request("POST", f"{API}/jobs/{identity}/cancel", headers=headers)
            self.assertEqual(cancelled.status_code, 200, cancelled.text)
            model.release.set()
            job = self.await_job(client, identity)
            self.assertEqual(job["status"], "cancelled", job)
            self.assertNotIn("filled.pdf", job["files"])
            download = client.request("GET", f"{API}/jobs/{identity}/download/filled.pdf")
            self.assertEqual(download.status_code, 404)
            self.assertEqual(self.questionnaire.read_bytes(), self.original_bytes)
        finally:
            model.release.set()
            client.close()
            service.close()
            model.close()


if __name__ == "__main__":
    unittest.main()
