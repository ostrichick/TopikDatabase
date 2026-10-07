"""Reject operational targets and false physical-device evidence before writes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import stage9_physical_check as physical


class PhysicalCutoverGuards(unittest.TestCase):
    def test_operational_database_is_rejected_before_source_or_db_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "connection.json").write_text(json.dumps({"database_url": "postgresql://app@host/topik"}))
            with patch.object(physical, "verify_immutable_source") as verify, patch.object(physical, "ReviewStore") as store:
                with self.assertRaisesRegex(RuntimeError, "disposable"):
                    physical.run("pc-write", root)
            verify.assert_not_called()
            store.assert_not_called()

    def test_laptop_cannot_be_presented_as_physical_pc(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "connection.json").write_text(json.dumps({"database_url": "postgresql://app@host/" + physical.TARGET}))
            with patch.object(physical.socket, "gethostname", return_value="DUBUYOGA"), \
                    patch.object(physical, "verify_immutable_source") as verify:
                with self.assertRaisesRegex(RuntimeError, "wrong physical"):
                    physical.run("pc-write", root)
            verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
