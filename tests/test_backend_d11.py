import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.api.main import create_app
from backend.core.config import Settings
from backend.core.errors import AppError
from backend.storage import database


class BackendD11Tests(unittest.TestCase):
    def settings(self, directory: str) -> Settings:
        return Settings(database_path=Path(directory) / "database" / "app.sqlite3")

    def test_empty_database_and_repeat_migration_are_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            self.assertEqual(database.migrate(settings), 12)
            self.assertEqual(database.migrate(settings), 12)
            with database.connection_scope(settings) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0], 12)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM app_metadata").fetchone()[0], 0)

    def test_connection_pragmas_and_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            database.migrate(settings)
            with database.connection_scope(settings) as connection:
                info = database.sqlite_runtime_info(connection)
                self.assertTrue(info["foreign_keys"])
                self.assertEqual(str(info["journal_mode"]).lower(), "wal")
                self.assertEqual(info["busy_timeout_ms"], 5000)
                self.assertEqual(info["synchronous"], 2)
            self.assertTrue(connection is not None)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_failed_migration_does_not_record_success_or_leave_partial_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            original = database.MIGRATIONS_DIR
            try:
                migration_dir = Path(directory) / "migrations"
                migration_dir.mkdir()
                (migration_dir / "001_bad.sql").write_text("CREATE TABLE partial (id INTEGER);\nINVALID SQL;\n")
                database.MIGRATIONS_DIR = migration_dir
                with self.assertRaises(AppError) as error:
                    database.migrate(settings)
                self.assertEqual(error.exception.body.code, "migration_failed")
                with database.connection_scope(settings) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0], 0)
                    self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())
            finally:
                database.MIGRATIONS_DIR = original

    def test_app_lifespan_migrates_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            app = create_app(settings)
            async def check_lifespan():
                async with app.router.lifespan_context(app):
                    with database.connection_scope(settings) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0], 12)
            import asyncio
            asyncio.run(check_lifespan())


if __name__ == "__main__":
    unittest.main()
