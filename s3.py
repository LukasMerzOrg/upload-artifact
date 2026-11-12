"""Minimal S3 client (SigV4, path-style) on the Python standard library.

Only what the action needs: put (single or multipart), presigned GET, and the
bucket lifecycle call used by setup_lifecycle.py. No third-party dependency, so
it runs on any runner that has python3.
"""
import base64
import datetime
import hashlib
import hmac
import http.client
import re
import urllib.parse

# Cloudflare's proxy rejects request bodies over 100 MB; stay well below it.
PART_SIZE = 32 * 1024 * 1024
MAX_PRESIGN_SECONDS = 7 * 24 * 3600  # SigV4 limit


def _quote(value, safe="-_.~"):
    return urllib.parse.quote(value, safe=safe)


def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


class S3:
    def __init__(self, endpoint, region, access_key, secret_key):
        url = urllib.parse.urlsplit(endpoint)
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError(f"endpoint must be an http(s) URL, got {endpoint!r}")
        self.scheme, self.host = url.scheme, url.netloc
        self.region, self.access_key, self.secret_key = region, access_key, secret_key

    # --- signing -----------------------------------------------------------

    def _signing_key(self, date):
        key = _hmac(("AWS4" + self.secret_key).encode(), date)
        for part in (self.region, "s3", "aws4_request"):
            key = _hmac(key, part)
        return key

    def _signature(self, method, path, query, headers, payload_hash, now):
        """Returns (signature, signed header names, credential scope)."""
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        scope = f"{amz_date[:8]}/{self.region}/s3/aws4_request"
        canonical_query = "&".join(
            f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
        names = sorted(h.lower() for h in headers)
        lowered = {k.lower(): v for k, v in headers.items()}
        canonical = "\n".join([
            method, _quote(path, safe="/-_.~"), canonical_query,
            "".join(f"{n}:{lowered[n].strip()}\n" for n in names),
            ";".join(names), payload_hash])
        to_sign = "\n".join([
            "AWS4-HMAC-SHA256", amz_date, scope,
            hashlib.sha256(canonical.encode()).hexdigest()])
        signature = hmac.new(self._signing_key(amz_date[:8]), to_sign.encode(),
                             hashlib.sha256).hexdigest()
        return signature, ";".join(names), scope

    def presign_get(self, bucket, key, expires, now=None):
        now = now or datetime.datetime.now(datetime.timezone.utc)
        path = f"/{bucket}/{key}"
        _, _, scope = self._signature("GET", path, {}, {"host": self.host}, "", now)
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"),
            "X-Amz-Expires": str(min(int(expires), MAX_PRESIGN_SECONDS)),
            "X-Amz-SignedHeaders": "host",
        }
        signature, _, _ = self._signature(
            "GET", path, query, {"host": self.host}, "UNSIGNED-PAYLOAD", now)
        query["X-Amz-Signature"] = signature
        qs = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
        return f"{self.scheme}://{self.host}{_quote(path, safe='/-_.~')}?{qs}"

    # --- requests ----------------------------------------------------------

    def request(self, method, path, query=None, body=b"", headers=None):
        query = query or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = dict(headers or {})
        headers.update({
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": now.strftime("%Y%m%dT%H%M%SZ"),
        })
        signature, signed, scope = self._signature(
            method, path, query, headers, payload_hash, now)
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{scope}, "
            f"SignedHeaders={signed}, Signature={signature}")
        headers["Content-Length"] = str(len(body))
        qs = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
        target = _quote(path, safe="/-_.~") + (f"?{qs}" if qs else "")
        cls = (http.client.HTTPSConnection if self.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(self.host, timeout=300)
        try:
            conn.request(method, target, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
        finally:
            conn.close()
        if resp.status >= 300:
            raise RuntimeError(
                f"S3 {method} {path} failed: HTTP {resp.status} "
                f"{data[:500].decode(errors='replace')}")
        return resp, data

    def put_file(self, bucket, key, file_path, content_type):
        """Uploads a file; multipart when it is larger than one part."""
        path = f"/{bucket}/{key}"
        with open(file_path, "rb") as f:
            first = f.read(PART_SIZE)
            second = f.read(PART_SIZE)
            if not second:
                self.request("PUT", path, body=first,
                             headers={"content-type": content_type})
                return
            _, data = self.request("POST", path, {"uploads": ""},
                                   headers={"content-type": content_type})
            upload_id = re.search(rb"<UploadId>([^<]+)</UploadId>", data).group(1).decode()
            etags, number, chunk, pending = [], 1, first, second
            try:
                while chunk:
                    resp, _ = self.request(
                        "PUT", path,
                        {"partNumber": str(number), "uploadId": upload_id}, body=chunk)
                    etags.append(resp.getheader("ETag"))
                    number += 1
                    chunk, pending = pending, (f.read(PART_SIZE) if pending else b"")
                parts = "".join(
                    f"<Part><PartNumber>{i}</PartNumber><ETag>{etag}</ETag></Part>"
                    for i, etag in enumerate(etags, 1))
                self.request(
                    "POST", path, {"uploadId": upload_id},
                    body=f"<CompleteMultipartUpload>{parts}</CompleteMultipartUpload>".encode())
            except BaseException:
                try:
                    self.request("DELETE", path, {"uploadId": upload_id})
                except Exception:
                    pass  # the lifecycle rule aborts leftover uploads
                raise

    def put_lifecycle(self, bucket, xml):
        body = xml.encode()
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        self.request("PUT", f"/{bucket}", {"lifecycle": ""}, body=body,
                     headers={"content-md5": md5})
