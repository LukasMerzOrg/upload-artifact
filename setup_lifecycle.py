"""One-time bucket setup: expire objects under each ttl-<days>d/ prefix.

Usage: GARAGE_ACCESS_KEY_ID=... GARAGE_SECRET_ACCESS_KEY=... \
       python3 setup_lifecycle.py [endpoint] [bucket] [region]
The key needs owner permission on the bucket.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from s3 import S3  # noqa: E402
from upload import TTL_DAYS  # noqa: E402


def lifecycle_xml():
    rules = "".join(
        f"<Rule><ID>ttl-{d}d</ID><Status>Enabled</Status>"
        f"<Filter><Prefix>ttl-{d}d/</Prefix></Filter>"
        f"<Expiration><Days>{d}</Days></Expiration></Rule>" for d in TTL_DAYS)
    rules += ("<Rule><ID>abort-incomplete-uploads</ID><Status>Enabled</Status>"
              "<Filter><Prefix></Prefix></Filter>"
              "<AbortIncompleteMultipartUpload><DaysAfterInitiation>1</DaysAfterInitiation>"
              "</AbortIncompleteMultipartUpload></Rule>")
    return f"<LifecycleConfiguration>{rules}</LifecycleConfiguration>"


if __name__ == "__main__":
    args = sys.argv[1:] + [None] * 3
    endpoint = args[0] or "https://s3.merz.tools"
    bucket = args[1] or "github"
    region = args[2] or "garage"
    S3(endpoint, region, os.environ["GARAGE_ACCESS_KEY_ID"],
       os.environ["GARAGE_SECRET_ACCESS_KEY"]).put_lifecycle(bucket, lifecycle_xml())
    print(f"Lifecycle rules set on {bucket}: expire after {', '.join(map(str, TTL_DAYS))} days")
