# upload-artifact

Drop-in replacement for `actions/upload-artifact` that stores artifacts in a
[Garage](https://garagehq.deuxfleurs.fr/) (S3) bucket instead of GitHub's
artifact storage, so they do not count against the storage quota. The job
summary gets a download link.

```yaml
- uses: LukasMerzOrg/upload-artifact@v1
  with:
    access-key-id: ${{ secrets.GARAGE_ACCESS_KEY_ID }}
    secret-access-key: ${{ secrets.GARAGE_SECRET_ACCESS_KEY }}
    name: debug-apk
    path: build/app-debug.apk
    retention-days: 3
```

Inputs `name`, `path`, `if-no-files-found`, `retention-days`,
`compression-level` and `include-hidden-files` behave like the official
action. `endpoint`, `bucket` and `region` default to this org's Garage.
Output: `artifact-url`.

## Behaviour

- One file is uploaded as it is; several files or a directory become `<name>.zip`.
- Key: `ttl-<days>d/<owner>/<repo>/<run id>-<attempt>/<name>...`.
- `retention-days` is rounded up to 3, 7, 14, 30 or 90 (default 90). Bucket
  lifecycle rules delete objects under each `ttl-<days>d/` prefix.
- The link in the job summary is presigned and valid for the retention time,
  at most 7 days (S3 limit).
- Uploads go in 32 MB parts, below Cloudflare's 100 MB request limit.
- Needs only `python3` on the runner.

## Setup

1. Bucket and a key with read, write and owner permission.
2. Lifecycle rules, once:
   `GARAGE_ACCESS_KEY_ID=... GARAGE_SECRET_ACCESS_KEY=... python3 setup_lifecycle.py`
3. Secrets `GARAGE_ACCESS_KEY_ID` and `GARAGE_SECRET_ACCESS_KEY` in each repository.
4. Migrate workflows: `python3 tools/migrate.py .github/workflows/*.yml`

Tests: `python3 -m unittest -v`
