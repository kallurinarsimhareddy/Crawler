"""S3-compatible result storage (Supabase Storage / Cloudflare R2), against moto's S3."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import boto3
    from moto import mock_aws
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("boto3/moto not installed")

from cloud.ops.stamps import StampMismatchError, ensure_storage_stamp
from cloud.shared.s3_storage import S3Storage
from cloud.shared.storage import InvalidKeyError


class TestS3Storage(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = mock_aws()
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="k", aws_secret_access_key="s")
        self.client.create_bucket(Bucket="careercloud-staging-results")
        self.storage = S3Storage(bucket="careercloud-staging-results", namespace="staging", client=self.client)
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.source = Path(scratch.name) / "jobs.csv"
        self.source.write_bytes(b"company,title\nAcme,Engineer\n")

    def test_round_trip_under_the_environment_namespace(self) -> None:
        key = "results/owner/job_abc/jobs.csv"
        stored = self.storage.put_file(key, self.source, content_type="text/csv")
        self.assertEqual(stored.size_bytes, self.source.stat().st_size)
        self.assertEqual(len(stored.sha256), 64)
        self.assertTrue(self.storage.exists(key))
        self.assertEqual(b"".join(self.storage.iter_bytes(key)), self.source.read_bytes())
        objects = [o["Key"] for o in self.client.list_objects_v2(Bucket="careercloud-staging-results")["Contents"]]
        self.assertEqual(objects, ["staging/results/owner/job_abc/jobs.csv"])
        self.storage.delete(key)
        self.assertFalse(self.storage.exists(key))
        with self.assertRaises(FileNotFoundError):
            self.storage.open(key)

    def test_keys_are_validated(self) -> None:
        with self.assertRaises(InvalidKeyError):
            self.storage.put_file("../production/x", self.source, content_type="text/plain")

    def test_environment_stamp(self) -> None:
        ensure_storage_stamp(self.storage, "staging")
        with self.assertRaises(StampMismatchError):
            ensure_storage_stamp(self.storage, "production")
        production = S3Storage(bucket="careercloud-staging-results", namespace="production", client=self.client)
        ensure_storage_stamp(production, "production")  # a different namespace is a different stamp

    def test_ping(self) -> None:
        self.storage.ping()


if __name__ == "__main__":
    unittest.main()
