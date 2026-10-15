from pathlib import Path
from datetime import datetime
import tempfile
import unittest

from scripts.collect_research_product_terms import collect, validate_pdf_bytes


class ProductArchiveTests(unittest.TestCase):
    def test_html_error_is_not_archived_as_a_pdf(self):
        with self.assertRaises(ValueError):
            validate_pdf_bytes(b"<html>Access denied</html>")

    def test_download_is_not_promoted_to_verified_terms(self):
        with tempfile.TemporaryDirectory() as folder:
            result = collect(Path(folder)/"new", fetch=lambda url: (b"%PDF-1.7\nfixture", {}))
            self.assertTrue(all(d["status"] == "downloaded_pending_identity_and_terms_review" for d in result["documents"]))
            self.assertEqual(len(result["documents"]), 4)
            self.assertTrue(all((Path(folder)/"new"/d["raw_file"]).exists() for d in result["documents"]))
            self.assertTrue(all(datetime.fromisoformat(d["fetched_at"]) >= datetime.fromisoformat(d["request_started_at"])
                                for d in result["documents"]))

    def test_failure_is_explicit_and_archive_cannot_be_overwritten(self):
        def fail(url):
            raise OSError("fixture unavailable")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"new"
            result = collect(path, fetch=fail)
            self.assertTrue(all(d["status"] == "failed" and "raw_file" not in d for d in result["documents"]))
            with self.assertRaises(FileExistsError):
                collect(path, fetch=fail)


if __name__ == "__main__":
    unittest.main()
