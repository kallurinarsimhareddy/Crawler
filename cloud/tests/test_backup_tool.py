"""deploy/ops/backup/sana_backup.py: the refusals that keep a backup from being the only copy."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOL = Path(__file__).resolve().parents[2] / "deploy" / "ops" / "backup" / "sana_backup.py"
spec = importlib.util.spec_from_file_location("sana_backup", TOOL)
sana_backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sana_backup)


class BackupRefusals(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def test_an_off_site_folder_is_required(self) -> None:
        with self.assertRaisesRegex(sana_backup.BackupError, "--offsite is required"):
            sana_backup.backup("postgresql://u:pw@db.example/x", pg_bin=self.root, out_dir=self.root / "out",
                               offsite=None)

    def test_the_off_site_copy_must_be_on_another_physical_disk(self) -> None:
        with mock.patch.object(sana_backup, "physical_disk", return_value="disk0"), \
                self.assertRaisesRegex(sana_backup.BackupError, "same physical disk"):
            sana_backup.backup("postgresql://u:pw@db.example/x", pg_bin=self.root, out_dir=self.root / "out",
                               offsite=self.root / "usb")

    def test_the_password_stays_off_the_command_line(self) -> None:
        env = sana_backup.pg_env("postgresql://alice:s3cret@db.example:5432/app?sslmode=require")
        self.assertEqual((env["PGUSER"], env["PGPASSWORD"], env["PGHOST"], env["PGSSLMODE"]),
                         ("alice", "s3cret", "db.example", "require"))
        self.assertNotIn("s3cret", sana_backup.describe("postgresql://alice:s3cret@db.example:5432/app"))

    def test_the_database_url_is_read_from_the_env_file(self) -> None:
        env_file = self.root / "x.env"
        env_file.write_text("OTHER=1\nCAREERCLOUD_DATABASE_URL=\"postgresql://h/db\"\n", encoding="utf-8")
        self.assertEqual(sana_backup.read_database_url(env_file), "postgresql://h/db")
        env_file.write_text("OTHER=1\n", encoding="utf-8")
        with self.assertRaises(sana_backup.BackupError):
            sana_backup.read_database_url(env_file)


if __name__ == "__main__":
    unittest.main()
