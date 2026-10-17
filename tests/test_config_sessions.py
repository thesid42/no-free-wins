from pathlib import Path
import tempfile
import unittest
from optimizer_core.config import DaytonaSettings, read_environment
from optimizer_core.sessions import load_session, prepare_session


class ConfigurationTests(unittest.TestCase):
    def test_values_are_literal_and_real_environment_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("DAYTONA_API_KEY='$(literal)'\nDAYTONA_TARGET=us\n")
            values = read_environment(path, environ={"DAYTONA_TARGET": "eu"})
            self.assertEqual(values["DAYTONA_API_KEY"], "$(literal)")
            self.assertEqual(values["DAYTONA_TARGET"], "eu")

    def test_duplicate_or_unclosed_environment_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            for content in ("DAYTONA_TARGET=a\nDAYTONA_TARGET=b", "DAYTONA_TARGET='a"):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    read_environment(path, environ={})

    def test_secret_never_in_repr_or_public_status(self):
        settings = DaytonaSettings(api_key="unit-secret-never-print")
        self.assertNotIn(settings.api_key, repr(settings))
        self.assertNotIn(settings.api_key, str(settings.public_status()))

    def test_api_url_credentials_or_non_https_rejected(self):
        for url in ("http://example.com", "https://user:secret@example.com",
                    "https://example.com?token=secret"):
            with self.assertRaises(ValueError):
                DaytonaSettings(api_url=url).validate(require_key=False)

    def test_session_seed_committed_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            first = prepare_session("unit-session", workload_lines=20, root=directory)
            second = prepare_session("unit-session", workload_lines=20, root=directory)
            self.assertEqual(first, second)
            self.assertNotIn("seed", first)
            self.assertEqual(len(load_session("unit-session", root=directory)["seed"]), 32)
            with self.assertRaises(ValueError):
                prepare_session("unit-session", workload_lines=30, root=directory)

    def test_session_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("../escape", "/absolute", "C:\\bad", ""):
                with self.assertRaises(ValueError):
                    prepare_session(name, root=directory)
