"""
End-to-end tests for the local HTTP API, the job manager, the database and
the CLI — driven against a real server on a loopback port.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bomiq.cli import main as cli_main  # noqa: E402
from bomiq.config import Config, Credentials  # noqa: E402
from bomiq.core.db import (  # noqa: E402
    Database, PartCache, ProjectStore, ResponseCache, TemplateStore,
)
from bomiq.engine import Engine  # noqa: E402
from bomiq.server.app import create_server  # noqa: E402
from bomiq.server.http import parse_multipart  # noqa: E402
from bomiq.server.jobs import JobManager  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def temp_config(**settings) -> Config:
    directory = Path(tempfile.mkdtemp(prefix="bomiq-srv-"))
    config = Config(data_dir=directory / "data", config_dir=directory / "cfg",
                    load_env=False, use_keyring=False)
    config.settings.offline = True
    config.settings.update(settings)
    return config


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "test.sqlite3"
        self.db = Database(self.path)

    def tearDown(self):
        self.db.close()

    def test_schema_created(self):
        rows = self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")
        names = {row["name"] for row in rows}
        for table in ("http_cache", "part_cache", "templates", "projects",
                      "audit", "header_learning", "part_notes"):
            self.assertIn(table, names)

    def test_response_cache_ttl(self):
        cache = ResponseCache(self.db)
        cache.set("k", {"status": 200, "headers": {}, "body": "x"}, ttl=60)
        self.assertIsNotNone(cache.get("k"))
        cache.set("expired", {"status": 200, "headers": {}, "body": "x"},
                  ttl=-1)
        self.assertIsNone(cache.get("expired"))

    def test_part_cache_keyed_per_provider(self):
        cache = PartCache(self.db)
        cache.set("digikey", "LM358DR", {"mpn": "LM358DR"})
        self.assertIsNotNone(cache.get("digikey", "LM358DR"))
        self.assertIsNone(cache.get("mouser", "LM358DR"))

    def test_template_store_round_trip(self):
        store = TemplateStore(self.db)
        template_id = store.save("My layout", "fp1", {"mpn": 0, "quantity": 1})
        found = store.find("fp1")
        self.assertEqual(found["id"], template_id)
        self.assertEqual(found["mapping"]["mpn"], 0)

    def test_template_use_count_increments(self):
        store = TemplateStore(self.db)
        store.save("L", "fp2", {"mpn": 0})
        store.save("L", "fp2", {"mpn": 0})
        self.assertEqual(store.find("fp2")["use_count"], 2)

    def test_header_learning_ranks_by_hits(self):
        store = TemplateStore(self.db)
        store.learn_header("weirdcol", "mpn")
        store.learn_header("weirdcol", "mpn")
        store.learn_header("weirdcol", "internal_pn")
        field, hits = store.learned_field("weirdcol")
        self.assertEqual(field, "mpn")
        self.assertEqual(hits, 2)

    def test_project_store(self):
        store = ProjectStore(self.db)
        pid = store.save("Proj", {"a": 1}, summary={"lines": 5})
        self.assertEqual(store.load(pid), {"a": 1})
        listing = store.list()
        self.assertEqual(listing[0]["summary"]["lines"], 5)
        store.delete(pid)
        self.assertIsNone(store.load(pid))

    def test_prune_and_stats(self):
        import time as _time

        self.db.execute(
            "INSERT INTO http_cache(key, payload, created_at, expires_at) "
            "VALUES(?,?,?,?)",
            ("old", "{}", _time.time() - 100, _time.time() - 10))
        removed = self.db.prune()
        self.assertGreaterEqual(removed["http_cache"], 1)
        stats = self.db.stats()
        self.assertIn("path", stats)

    def test_corrupt_database_is_rotated_not_fatal(self):
        path = Path(tempfile.mkdtemp()) / "corrupt.sqlite3"
        path.write_bytes(b"this is definitely not a database" * 40)
        db = Database(path)
        self.assertTrue(db.query("SELECT 1 AS one"))
        db.close()

    def test_concurrent_writes_from_threads(self):
        import threading

        cache = ResponseCache(self.db)
        errors: list[Exception] = []

        def worker(index: int) -> None:
            try:
                for n in range(10):
                    cache.set(f"k{index}-{n}",
                              {"status": 200, "headers": {}, "body": "x"},
                              ttl=60)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #

class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.credentials = Credentials(self.dir, use_keyring=False)

    def test_round_trip(self):
        self.credentials.set("mouser", {"api_key": "secret-value"})
        self.assertEqual(self.credentials.get("mouser", "api_key"),
                         "secret-value")
        self.assertTrue(self.credentials.is_configured("mouser"))

    def test_status_never_leaks_the_value(self):
        self.credentials.set("mouser", {"api_key": "supersecretkey123"})
        payload = json.dumps(self.credentials.status())
        self.assertNotIn("supersecretkey123", payload)
        self.assertIn("***", payload)

    def test_env_override(self):
        import os

        os.environ["MOUSER_API_KEY"] = "from-env"
        try:
            self.assertEqual(self.credentials.get("mouser", "api_key"),
                             "from-env")
        finally:
            del os.environ["MOUSER_API_KEY"]

    def test_clear(self):
        self.credentials.set("mouser", {"api_key": "x"})
        self.credentials.clear("mouser")
        self.assertFalse(self.credentials.is_configured("mouser"))

    def test_file_permissions_are_owner_only(self):
        import os
        import stat

        self.credentials.set("mouser", {"api_key": "x"})
        if os.name != "nt":
            mode = stat.S_IMODE(self.credentials.file.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)

    def test_multi_field_provider(self):
        self.credentials.set("digikey", {"client_id": "a",
                                         "client_secret": "b"})
        self.assertTrue(self.credentials.is_configured("digikey"))
        self.credentials.set("digikey", {"client_secret": ""})
        self.assertFalse(self.credentials.is_configured("digikey"))


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

class TestJobs(unittest.TestCase):
    def test_success(self):
        manager = JobManager()
        job = manager.submit("t", "label", lambda j: {"ok": True})
        self._wait(manager, job.id)
        self.assertEqual(manager.get(job.id).state, "done")
        self.assertEqual(manager.get(job.id).result, {"ok": True})

    def test_failure_is_captured(self):
        manager = JobManager()

        def boom(job):
            raise ValueError("nope")

        job = manager.submit("t", "label", boom)
        self._wait(manager, job.id)
        self.assertEqual(manager.get(job.id).state, "error")
        self.assertIn("nope", manager.get(job.id).error)

    def test_cancellation(self):
        manager = JobManager()

        def slow(job):
            for _ in range(100):
                job.cancel.check()
                time.sleep(0.01)
            return "finished"

        job = manager.submit("t", "label", slow)
        time.sleep(0.05)
        self.assertTrue(manager.cancel(job.id))
        self._wait(manager, job.id)
        self.assertEqual(manager.get(job.id).state, "cancelled")

    def test_progress_events_recorded(self):
        from bomiq.engine import Progress

        manager = JobManager()

        def work(job):
            report = manager.progress_callback(job)
            report(Progress(stage="ingest", message="one", percent=10))
            report(Progress(stage="enrich", message="two", percent=50))
            return True

        job = manager.submit("t", "l", work)
        self._wait(manager, job.id)
        events = manager.get(job.id).events
        self.assertEqual([e["message"] for e in events], ["one", "two"])

    def test_unknown_job(self):
        self.assertIsNone(JobManager().get("nope"))

    @staticmethod
    def _wait(manager, job_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = manager.get(job_id)
            if job and job.state in ("done", "error", "cancelled"):
                return job
            time.sleep(0.02)
        raise AssertionError("job did not finish")


# --------------------------------------------------------------------------- #
# Multipart
# --------------------------------------------------------------------------- #

class TestMultipart(unittest.TestCase):
    def build(self, filename="bom.csv", content=b"MPN,Qty\nLM358DR,1\n",
              extra_fields=None):
        boundary = "----bomiqtest"
        parts = []
        for key, value in (extra_fields or {}).items():
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n".encode())
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: text/csv\r\n\r\n".encode() + content + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    def test_parses_a_file(self):
        body, content_type = self.build()
        fields, files = parse_multipart(body, content_type)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "bom.csv")
        self.assertEqual(files[0].data, b"MPN,Qty\nLM358DR,1\n")
        self.assertEqual(fields, {})

    def test_parses_fields_alongside_the_file(self):
        body, content_type = self.build(extra_fields={"build": "500"})
        fields, files = parse_multipart(body, content_type)
        self.assertEqual(fields["build"], "500")
        self.assertEqual(len(files), 1)

    def test_path_traversal_in_the_filename_is_neutralised(self):
        body, content_type = self.build(filename="../../etc/passwd")
        _, files = parse_multipart(body, content_type)
        self.assertNotIn("/", files[0].filename)
        self.assertNotIn("..", files[0].filename)

    def test_binary_content_survives(self):
        payload = bytes(range(256)) * 4
        body, content_type = self.build(filename="b.bin", content=payload)
        _, files = parse_multipart(body, content_type)
        self.assertEqual(files[0].data, payload)

    def test_missing_boundary_raises(self):
        from bomiq.server.http import HttpError

        with self.assertRaises(HttpError):
            parse_multipart(b"x", "multipart/form-data")


# --------------------------------------------------------------------------- #
# The HTTP API
# --------------------------------------------------------------------------- #

class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = temp_config(build_quantity=100)
        cls.server = create_server(cls.config, host="127.0.0.1", port=0)
        cls.base = cls.server.start_background()
        cls.token = cls.server.token

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.state["engine"].close()

    # -- helpers ---------------------------------------------------------- #

    def call(self, path, method="GET", payload=None, token=True,
             raw=None, content_type=None, expect=None):
        url = self.base + path
        data = None
        headers = {}
        if (payload is not None or raw is not None) and method == "GET":
            method = "POST"
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        elif raw is not None:
            data = raw
            if content_type:
                headers["Content-Type"] = content_type
        if token:
            headers["X-BOMIQ-Token"] = self.token
        request = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
        if expect is not None:
            self.assertEqual(status, expect, body[:400])
        try:
            return status, json.loads(body) if body else None
        except json.JSONDecodeError:
            return status, body

    # -- tests ------------------------------------------------------------ #

    def test_ping_is_public(self):
        status, payload = self.call("/api/ping", token=False)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_token_is_required(self):
        status, _ = self.call("/api/status", token=False)
        self.assertEqual(status, 401)

    def test_bad_token_rejected(self):
        request = urllib.request.Request(
            self.base + "/api/status",
            headers={"X-BOMIQ-Token": "wrong"})
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(context.exception.code, 401)

    def test_cross_origin_is_rejected(self):
        request = urllib.request.Request(
            self.base + "/api/status",
            headers={"X-BOMIQ-Token": self.token,
                     "Origin": "https://evil.example.com"})
        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(context.exception.code, 403)

    def test_bootstrap_shape(self):
        status, payload = self.call("/api/bootstrap", expect=200)
        for key in ("app", "settings", "providers", "fields", "rules",
                    "file_support", "export_formats", "currencies"):
            self.assertIn(key, payload)
        self.assertGreater(len(payload["fields"]), 10)

    def test_static_index_is_served(self):
        request = urllib.request.Request(self.base + "/")
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode()
        self.assertIn("BOM-IQ", body)

    def test_static_traversal_blocked(self):
        status, _ = self.call("/../../etc/passwd", token=False)
        self.assertIn(status, (403, 404))

    def test_unknown_route(self):
        status, _ = self.call("/api/nope", expect=404)

    def test_method_not_allowed(self):
        status, payload = self.call("/api/ping", method="POST")
        self.assertEqual(status, 405)
        self.assertIn("allow", payload.get("detail", {}))

    def test_invalid_json_body(self):
        status, _ = self.call("/api/settings", method="POST",
                              raw=b"{not json", content_type="application/json")
        self.assertEqual(status, 400)

    def test_settings_round_trip(self):
        status, payload = self.call("/api/settings", payload={
            "build_quantity": 250, "currency": "EUR"}, expect=200)
        self.assertIn("build_quantity", payload["changed"])
        status, payload = self.call("/api/settings", expect=200)
        self.assertEqual(payload["build_quantity"], 250)
        self.assertEqual(payload["currency"], "EUR")
        # restore
        self.call("/api/settings", payload={"build_quantity": 100,
                                            "currency": "USD"})

    def test_unknown_setting_is_ignored_not_fatal(self):
        status, payload = self.call("/api/settings",
                                    payload={"nonsense_key": 1}, expect=200)
        self.assertEqual(payload["changed"], [])

    def test_credentials_endpoint_validates_fields(self):
        status, _ = self.call("/api/providers/mouser/credentials",
                              payload={"credentials": {"wrong_field": "x"}})
        self.assertEqual(status, 400)

    def test_credentials_unknown_provider(self):
        status, _ = self.call("/api/providers/nosuch/credentials",
                              payload={"credentials": {"api_key": "x"}})
        self.assertEqual(status, 404)

    def test_full_analysis_flow(self):
        content = (SAMPLES / "01-altium-export.csv").read_bytes()
        status, upload = self.call(
            "/api/upload?filename=01-altium-export.csv", method="POST",
            raw=content, content_type="text/csv", expect=200)
        self.assertTrue(upload["ok"])
        self.assertGreater(upload["bom"]["line_count"], 10)
        upload_id = upload["upload_id"]

        status, started = self.call("/api/analyse", payload={
            "upload_id": upload_id, "settings": {"build_quantity": 100}})
        self.assertEqual(status, 202)
        job_id = started["job"]["id"]

        deadline = time.time() + 60
        state = ""
        while time.time() < deadline:
            _, payload = self.call(f"/api/jobs/{job_id}", expect=200)
            state = payload["job"]["state"]
            if state in ("done", "error", "cancelled"):
                break
            time.sleep(0.1)
        self.assertEqual(state, "done", payload["job"].get("error"))

        _, result = self.call(f"/api/jobs/{job_id}/result", expect=200)
        analysis_id = result["analysis_id"]
        analysis = result["analysis"]
        self.assertGreater(analysis["health"]["score"], 0)
        self.assertGreater(len(analysis["results"]), 10)
        self.assertTrue(analysis["offline"])

        # every export format must produce bytes
        for fmt in ("xlsx", "csv", "issues-csv", "quote-csv", "json",
                    "json-flat", "html"):
            request = urllib.request.Request(
                f"{self.base}/api/analysis/{analysis_id}/export?format={fmt}",
                headers={"X-BOMIQ-Token": self.token})
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
            self.assertGreater(len(body), 200, fmt)

        # unknown format
        status, _ = self.call(
            f"/api/analysis/{analysis_id}/export?format=pdf")
        self.assertEqual(status, 400)

        # save and reload as a project
        status, saved = self.call("/api/projects", payload={
            "analysis_id": analysis_id, "name": "Test project"}, expect=200)
        project_id = saved["project_id"]
        _, reopened = self.call(f"/api/projects/{project_id}", expect=200)
        self.assertEqual(len(reopened["analysis"]["results"]),
                         len(analysis["results"]))
        self.call(f"/api/projects/{project_id}", method="DELETE")

        # price comparison against the prices in the source file
        _, comparison = self.call(
            f"/api/analysis/{analysis_id}/price-comparison", expect=200)
        self.assertIn("compared_lines", comparison)

    def test_remap_applies_a_forced_mapping(self):
        content = b"ColA,ColB,ColC\nLM358DR,2,U1\nBAT54S,1,D1\n"
        status, upload = self.call(
            "/api/upload?filename=weird.csv", method="POST", raw=content,
            content_type="text/csv", expect=200)
        upload_id = upload["upload_id"]
        status, remapped = self.call("/api/remap", payload={
            "upload_id": upload_id,
            "mapping": {"mpn": 0, "quantity": 1, "ref_designators": 2},
        }, expect=200)
        self.assertEqual(remapped["bom"]["line_count"], 2)
        self.assertEqual(remapped["bom"]["lines"][0]["mpn"], "LM358DR")

    def test_upload_rejects_an_empty_body(self):
        status, _ = self.call("/api/upload", method="POST", raw=b"",
                              content_type="text/csv")
        self.assertEqual(status, 400)

    def test_unparseable_upload_returns_a_helpful_payload(self):
        status, payload = self.call(
            "/api/upload?filename=prose.csv", method="POST",
            raw=b"this file has nothing resembling a BOM in it\n",
            content_type="text/csv", expect=200)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])
        self.assertTrue(payload["hint"])

    def test_expired_upload_id(self):
        status, _ = self.call("/api/remap", payload={
            "upload_id": "does-not-exist", "mapping": {}})
        self.assertEqual(status, 404)

    def test_part_lookup(self):
        status, payload = self.call("/api/part?mpn=LM358DR", expect=200)
        self.assertTrue(payload["found"])
        self.assertEqual(payload["part"]["mpn"], "LM358DR")

    def test_part_lookup_requires_an_mpn(self):
        status, _ = self.call("/api/part")
        self.assertEqual(status, 400)

    def test_search(self):
        status, payload = self.call("/api/search",
                                    payload={"query": "CAP CER 0603"},
                                    expect=200)
        self.assertGreater(payload["count"], 0)

    def test_alternates_endpoint(self):
        status, payload = self.call("/api/alternates",
                                    payload={"mpn": "MAX3232ECPE+"},
                                    expect=200)
        self.assertTrue(payload["found"])

    def test_provider_test_endpoint(self):
        status, payload = self.call("/api/providers/test",
                                    payload={"providers": ["mock"]},
                                    expect=200)
        self.assertTrue(payload["results"][0]["ok"])

    def test_reference_endpoints(self):
        for path, key in (("/api/fields", "fields"), ("/api/rules", "rules"),
                          ("/api/templates", "templates")):
            status, payload = self.call(path, expect=200)
            self.assertIn(key, payload)

    def test_cache_endpoints(self):
        status, payload = self.call("/api/cache/prune", payload={}, expect=200)
        self.assertIn("removed", payload)
        status, payload = self.call("/api/cache/clear", payload={}, expect=200)
        self.assertTrue(payload["ok"])

    def test_status_endpoint(self):
        status, payload = self.call("/api/status", expect=200)
        self.assertIn("version", payload)
        self.assertIn("database", payload)

    def test_audit_trail(self):
        status, payload = self.call("/api/audit", expect=200)
        self.assertIn("entries", payload)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

class TestCli(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bomiq-cli-"))
        self.common = ["--quiet", "--data-dir", str(self.dir / "data"),
                       "--config-dir", str(self.dir / "cfg")]

    def run_cli(self, *args):
        from contextlib import redirect_stderr, redirect_stdout

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(self.common + list(args))
        return code, out.getvalue(), err.getvalue()

    def test_doctor(self):
        code, out, _ = self.run_cli("doctor")
        self.assertIn("Python version", out)

    def test_inspect(self):
        code, out, _ = self.run_cli(
            "inspect", str(SAMPLES / "01-altium-export.csv"))
        self.assertEqual(code, 0)
        self.assertIn("Column mapping", out)
        self.assertIn("RC0603FR-0710KL", out)

    def test_inspect_json(self):
        code, out, _ = self.run_cli(
            "inspect", str(SAMPLES / "06-iot-gateway.json"), "--json")
        payload = json.loads(out)
        self.assertIn("mapping", payload)

    def test_analyse_offline(self):
        code, out, _ = self.run_cli(
            "analyse", str(SAMPLES / "01-altium-export.csv"), "--offline",
            "--build", "50")
        self.assertEqual(code, 0)
        self.assertIn("Health", out)

    def test_analyse_with_exports(self):
        out_dir = self.dir / "reports"
        code, out, _ = self.run_cli(
            "analyse", str(SAMPLES / "06-iot-gateway.json"), "--offline",
            "--export", "xlsx,csv,html,json", "-o", str(out_dir))
        self.assertEqual(code, 0)
        produced = {p.suffix for p in out_dir.iterdir()}
        self.assertEqual(produced, {".xlsx", ".csv", ".html", ".json"})

    def test_min_health_gate(self):
        code, _, err = self.run_cli(
            "analyse", str(SAMPLES / "04-excel-damaged.csv"), "--offline",
            "--min-health", "99.9")
        self.assertEqual(code, 3)
        self.assertIn("below the required", err)

    def test_fail_on_error_gate(self):
        code, _, err = self.run_cli(
            "analyse", str(SAMPLES / "04-excel-damaged.csv"), "--offline",
            "--fail-on", "error")
        self.assertEqual(code, 5)

    def test_missing_file_is_a_clean_error(self):
        code, _, err = self.run_cli("analyse", "/no/such/file.csv")
        self.assertEqual(code, 2)
        self.assertIn("not found", err.lower())

    def test_unknown_export_format(self):
        code, _, err = self.run_cli(
            "analyse", str(SAMPLES / "06-iot-gateway.json"), "--offline",
            "--export", "pdf")
        self.assertEqual(code, 2)
        self.assertIn("Unknown export format", err)

    def test_part_and_search(self):
        code, out, _ = self.run_cli("part", "LM358DR")
        self.assertEqual(code, 0)
        self.assertIn("Texas Instruments", out)
        code, out, _ = self.run_cli("search", "CAP", "CER", "0603")
        self.assertEqual(code, 0)

    def test_part_not_found_exit_code(self):
        code, out, _ = self.run_cli("part", "??")
        self.assertEqual(code, 1)

    def test_providers_listing(self):
        code, out, _ = self.run_cli("providers")
        self.assertIn("mock", out)
        self.assertIn("DigiKey", out)

    def test_providers_set_and_enable(self):
        code, out, _ = self.run_cli(
            "providers", "--set", "mouser.api_key=testkey123",
            "--enable", "mouser")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("providers")
        # the key is stored but never echoed
        self.assertNotIn("testkey123", out)

    def test_providers_unknown_enable_rejected(self):
        code, out, _ = self.run_cli("providers", "--enable", "nosuch")
        self.assertEqual(code, 2)

    def test_config_show_and_set(self):
        code, out, _ = self.run_cli("config", "show")
        self.assertIn("build_quantity", out)
        code, out, _ = self.run_cli("config", "set", "build_quantity=777")
        self.assertIn("build_quantity", out)
        code, out, _ = self.run_cli("config", "show")
        self.assertIn("777", out)

    def test_projects_save_list_and_delete(self):
        code, out, _ = self.run_cli(
            "analyse", str(SAMPLES / "06-iot-gateway.json"), "--offline",
            "--save", "CLI project")
        self.assertEqual(code, 0)
        match = [line for line in out.splitlines() if "Saved as project" in line]
        self.assertTrue(match)
        project_id = match[0].split()[-1]
        code, out, _ = self.run_cli("projects", "list")
        self.assertIn("CLI project", out)
        code, out, _ = self.run_cli("projects", "show", project_id)
        self.assertIn("Health", out)
        code, out, _ = self.run_cli("projects", "delete", project_id)
        self.assertEqual(code, 0)

    def test_cache_stats(self):
        code, out, _ = self.run_cli("cache", "stats")
        self.assertIn("part_cache", out)


# --------------------------------------------------------------------------- #
# Engine-level behaviour
# --------------------------------------------------------------------------- #

class TestEngine(unittest.TestCase):
    def setUp(self):
        self.config = temp_config(build_quantity=100)
        self.engine = Engine(self.config)

    def tearDown(self):
        self.engine.close()

    def test_progress_reaches_one_hundred_percent(self):
        events = []
        self.engine.analyse_file(SAMPLES / "06-iot-gateway.json",
                                 progress=events.append)
        self.assertGreater(len(events), 3)
        self.assertEqual(round(events[-1].percent), 100)
        stages = {event.stage for event in events}
        self.assertTrue({"ingest", "enrich", "analyse", "finalise"} <= stages)

    def test_cancellation_raises(self):
        from bomiq.core.errors import CancelledError
        from bomiq.engine import CancelToken

        token = CancelToken()
        token.cancel()
        with self.assertRaises(CancelledError):
            self.engine.analyse_file(SAMPLES / "01-altium-export.csv",
                                     cancel=token)

    def test_offline_warning_is_always_present(self):
        analysis = self.engine.analyse_file(SAMPLES / "06-iot-gateway.json")
        self.assertTrue(analysis.offline)
        self.assertTrue(any("offline" in w.lower()
                            for w in analysis.warnings))

    def test_rules_are_recorded_on_the_analysis(self):
        analysis = self.engine.analyse_file(SAMPLES / "06-iot-gateway.json")
        self.assertIn("require_rohs", analysis.summary.rules)

    def test_build_quantity_scales_the_total(self):
        small = Engine(temp_config(build_quantity=10, offline=True))
        large = Engine(temp_config(build_quantity=1000, offline=True))
        try:
            a = small.analyse_file(SAMPLES / "06-iot-gateway.json")
            b = large.analyse_file(SAMPLES / "06-iot-gateway.json")
            self.assertGreater(b.summary.total_cost, a.summary.total_cost)
        finally:
            small.close()
            large.close()

    def test_every_sample_analyses_without_raising(self):
        for sample in sorted(SAMPLES.glob("0*")):
            if sample.name.endswith(".py"):
                continue
            with self.subTest(sample=sample.name):
                analysis = self.engine.analyse_file(sample)
                self.assertGreater(analysis.summary.total_lines, 0)
                self.assertGreaterEqual(analysis.health.score, 0)
                self.assertLessEqual(analysis.health.score, 100)
                # the model must serialise and come back intact
                reloaded = BomAnalysisRoundTrip(analysis)
                self.assertEqual(len(reloaded.results),
                                 len(analysis.results))


def BomAnalysisRoundTrip(analysis):
    from bomiq.core.models import BomAnalysis as Model

    return Model.from_dict(json.loads(json.dumps(analysis.to_dict(),
                                                 default=str)))


if __name__ == "__main__":
    unittest.main()
