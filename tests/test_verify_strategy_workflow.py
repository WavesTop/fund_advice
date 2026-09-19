from __future__ import annotations

import unittest

from scripts.verify_strategy_workflow import EMPTY_FILE_SHA256, durable_content_fingerprints


def fingerprint(*, size: int, digest: str, mtime_ns: int = 1) -> dict[str, int | str]:
    return {"size": size, "sha256": digest, "mtime_ns": mtime_ns}


class DurableContentFingerprintsTest(unittest.TestCase):
    def test_absent_and_empty_wal_have_the_same_durable_content(self) -> None:
        database = fingerprint(size=4, digest="database")

        before = durable_content_fingerprints({"database": database, "-wal": None, "-shm": None})
        after = durable_content_fingerprints({
            "database": {**database, "mtime_ns": 2},
            "-wal": fingerprint(size=0, digest=EMPTY_FILE_SHA256, mtime_ns=2),
            "-shm": fingerprint(size=32768, digest="reader-coordination", mtime_ns=2),
        })

        self.assertEqual(before, after)
        self.assertEqual(after["-wal"], {"state": "empty_or_absent"})

    def test_non_empty_wal_change_is_durable_activity(self) -> None:
        database = fingerprint(size=4, digest="database")
        before = durable_content_fingerprints({
            "database": database,
            "-wal": fingerprint(size=64, digest="wal-before"),
        })
        after = durable_content_fingerprints({
            "database": database,
            "-wal": fingerprint(size=64, digest="wal-after"),
        })

        self.assertNotEqual(before, after)

    def test_database_content_change_is_durable_activity(self) -> None:
        before = durable_content_fingerprints({
            "database": fingerprint(size=4, digest="database-before"),
            "-wal": None,
        })
        after = durable_content_fingerprints({
            "database": fingerprint(size=5, digest="database-after"),
            "-wal": None,
        })

        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
