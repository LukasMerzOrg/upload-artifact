"""Run with: python3 -m unittest -v"""
import datetime
import http.server
import os
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

import s3 as s3mod
import upload
from s3 import S3


class Signing(unittest.TestCase):
    def test_presign_matches_aws_documented_example(self):
        # "Authenticating Requests: Using Query Parameters" example in the S3 docs.
        client = S3("https://examplebucket.s3.amazonaws.com", "us-east-1",
                    "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        now = datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)
        # Virtual-host style in the example: the path is just /test.txt.
        with mock.patch.object(S3, "presign_get", S3.presign_get):
            path_sig, _, _ = client._signature(
                "GET", "/test.txt",
                {"X-Amz-Algorithm": "AWS4-HMAC-SHA256",
                 "X-Amz-Credential": "AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request",
                 "X-Amz-Date": "20130524T000000Z", "X-Amz-Expires": "86400",
                 "X-Amz-SignedHeaders": "host"},
                {"host": "examplebucket.s3.amazonaws.com"}, "UNSIGNED-PAYLOAD", now)
        self.assertEqual(
            path_sig, "aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")

    def test_presign_caps_expiry_at_seven_days(self):
        url = S3("https://s3.example", "garage", "k", "s").presign_get("b", "a b/c.zip", 10**9)
        self.assertIn("X-Amz-Expires=604800", url)
        self.assertTrue(url.startswith("https://s3.example/b/a%20b/c.zip?"))


class Ttl(unittest.TestCase):
    def test_rounds_up_to_bucket(self):
        self.assertEqual([upload.ttl_for(x) for x in ("", "1", "3", "4", "14", "15", "90", "400")],
                         [90, 3, 3, 7, 14, 30, 90, 90])


class FindFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = os.path.realpath(self.tmp.name)
        for rel in ("build/app.apk", "build/tour/a.png", "build/tour/sub/b.png",
                    "build/tour/.hidden", "x/TestResults/r.trx", "y/TestResults/s.trx"):
            path = os.path.join(self.ws, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def rel(self, files, root):
        return [os.path.relpath(f, root).replace(os.sep, "/") for f in files]

    def test_single_file(self):
        files, root = upload.find_files("build/app.apk", self.ws)
        self.assertEqual(self.rel(files, root), ["app.apk"])

    def test_directory_is_relative_to_itself_and_skips_hidden(self):
        files, root = upload.find_files("build/tour", self.ws)
        self.assertEqual(self.rel(files, root), ["a.png", "sub/b.png"])
        files, root = upload.find_files("build/tour", self.ws, include_hidden=True)
        self.assertEqual(self.rel(files, root), [".hidden", "a.png", "sub/b.png"])

    def test_recursive_glob_multiline_and_exclude(self):
        files, root = upload.find_files("**/TestResults/*.trx\n!y/**", self.ws)
        self.assertEqual(self.rel(files, root), ["r.trx"])
        files, root = upload.find_files("**/TestResults/*.trx\nbuild/app.apk", self.ws)
        self.assertEqual(self.rel(files, root),
                         ["build/app.apk", "x/TestResults/r.trx", "y/TestResults/s.trx"])

    def test_no_match(self):
        self.assertEqual(upload.find_files("nope/*.bin", self.ws)[0], [])

    def test_zip_layout(self):
        files, root = upload.find_files("build/tour", self.ws)
        dest = os.path.join(self.ws, "out.zip")
        upload.make_zip(files, root, dest, 6)
        self.assertEqual(sorted(zipfile.ZipFile(dest).namelist()), ["a.png", "sub/b.png"])


class FakeS3(http.server.BaseHTTPRequestHandler):
    """Records requests; answers like S3 for put and multipart."""
    log = []

    def _handle(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        FakeS3.log.append((self.command, self.path, len(body), self.headers.get("Authorization", "")))
        out = b"<R><UploadId>up-1</UploadId></R>" if self.path.endswith("?uploads=") else b""
        self.send_response(200)
        self.send_header("ETag", '"etag"')
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_PUT = do_POST = do_DELETE = _handle

    def log_message(self, *args):
        pass


class Upload(unittest.TestCase):
    def setUp(self):
        FakeS3.log = []
        self.server = http.server.HTTPServer(("127.0.0.1", 0), FakeS3)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.client = S3(f"http://127.0.0.1:{self.server.server_port}", "garage", "k", "s")
        self.tmp = tempfile.TemporaryDirectory()
        self.file = os.path.join(self.tmp.name, "f.bin")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_small_file_is_one_put(self):
        with open(self.file, "wb") as f:
            f.write(b"x" * 10)
        self.client.put_file("b", "k/f.bin", self.file, "application/octet-stream")
        self.assertEqual([(m, p, n) for m, p, n, _ in FakeS3.log], [("PUT", "/b/k/f.bin", 10)])
        self.assertTrue(FakeS3.log[0][3].startswith("AWS4-HMAC-SHA256 Credential=k/"))

    def test_large_file_is_multipart(self):
        with open(self.file, "wb") as f:
            f.write(b"x" * 25)
        with mock.patch.object(s3mod, "PART_SIZE", 10):
            self.client.put_file("b", "k/f.bin", self.file, "application/octet-stream")
        self.assertEqual(
            [(m, p, n) for m, p, n, _ in FakeS3.log[:-1]],
            [("POST", "/b/k/f.bin?uploads=", 0),
             ("PUT", "/b/k/f.bin?partNumber=1&uploadId=up-1", 10),
             ("PUT", "/b/k/f.bin?partNumber=2&uploadId=up-1", 10),
             ("PUT", "/b/k/f.bin?partNumber=3&uploadId=up-1", 5)])
        self.assertEqual(FakeS3.log[-1][:2], ("POST", "/b/k/f.bin?uploadId=up-1"))

    def test_exact_multiple_of_part_size(self):
        with open(self.file, "wb") as f:
            f.write(b"x" * 20)
        with mock.patch.object(s3mod, "PART_SIZE", 10):
            self.client.put_file("b", "k", self.file, "application/octet-stream")
        self.assertEqual([n for m, _, n, _ in FakeS3.log if m == "PUT"], [10, 10])


if __name__ == "__main__":
    unittest.main()
