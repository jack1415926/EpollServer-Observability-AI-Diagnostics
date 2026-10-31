import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from import_experiment import prepare_report, read_json, upload
from experiment_fixture import valid_report


class ImportExperimentTests(unittest.TestCase):
    def test_balanced_timing_cannot_be_silently_imported_as_legacy_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = valid_report()
            result, resources = root / 'result.json', root / 'resources.json'
            resources.write_text('{}')
            for method in ('verified_worker_quota_v1', 'verified_worker_range_v1'):
                result.write_text(json.dumps({**fixture['benchmark'], 'allocation': {'method': method}}))
                with self.subTest(method=method), self.assertRaisesRegex(ValueError, 'new experiment schema'):
                    prepare_report('balanced', result, resources, fixture['context'])

    def test_report_preserves_result_and_binds_resources_by_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = valid_report()
            result, resources = root / "result.json", root / "resources.json"
            result.write_text(json.dumps(fixture["benchmark"]))
            resources.write_text(json.dumps({"label": "fixture", "samples": ["raw samples remain local"],
                                              "started_utc": "2026-10-23T23:59:59+00:00",
                                              "command": ["test-only", "fixture"],
                                              **fixture["resources"]}))
            report = prepare_report("import check", result, resources, fixture["context"])
            self.assertEqual(report["benchmark"], fixture["benchmark"])
            self.assertNotIn("samples", report["resources"])
            self.assertEqual(report["context"]["resources_sha256"], hashlib.sha256(resources.read_bytes()).hexdigest())
            resources.write_text(resources.read_text().replace('"fixture"', '"other-run"'))
            with self.assertRaises(ValueError):
                prepare_report("import check", result, resources, fixture["context"])

    def test_rejects_same_label_from_another_run(self):
        fixture = valid_report()
        raw = {"label": "fixture", "started_utc": "2026-10-23T23:59:59+00:00",
               "command": ["test-only", "fixture"], **fixture["resources"]}
        with tempfile.TemporaryDirectory() as directory:
            result, resources = Path(directory) / "result.json", Path(directory) / "resources.json"
            result.write_text(json.dumps(fixture["benchmark"]))
            for key, value in (("started_utc", "2000-01-01T00:00:00+00:00"),
                               ("started_utc", "2026-10-25T00:00:00+00:00"),
                               ("started_utc", "2026-10-23T23:59:59"),
                               ("command", ["different-command"]), ("duration_sec", -1)):
                changed = {**raw, key: value}
                resources.write_text(json.dumps(changed))
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare_report("wrong pairing", result, resources, fixture["context"])

    def test_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"name":"a","name":"b"}')
            with self.assertRaises(ValueError):
                read_json(path)
            with path.open("wb") as stream:
                stream.truncate(8 * 1024 * 1024 + 1)
            with self.assertRaises(ValueError):
                read_json(path)

    def test_upload_rejects_nonlocal_and_embedded_credentials_without_network(self):
        for url in ("https://127.0.0.1:8080", "http://example.com", "http://user:pass@localhost:8080", "http://localhost:8080/elsewhere", "http://localhost:8080/?token=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                upload(url, {}, "not-sent")
