# Da'wah durable audio: upload, validation and delivery (DAWAH-02)

Authority: D2 "R2 upload validation and delivery", D0 A04, D5 R2 settings.
Code: `dawah/storage/` (port + S3/R2 adapter + in-memory double),
`dawah/uploads.py` (intent/finalize/promotion/cleanup), `dawah/media.py`
(MP3 validation), `dawah/management/commands/dawah_storage_cleanup.py`.

## Protocol

1. Admin creates a draft `aud` resource (DAWAH-01).
2. `POST /dawah/events/{event_id}/resources/{id}/audio/` with
   `{"file_name":"khutbah.mp3","content_type":"audio/mpeg","size_bytes":12000000}`
   → 201 `{upload_id, resource_id, method:"PUT", upload_url, headers:{"Content-Type":"audio/mpeg"}, expires_at, max_size_bytes}`.
   Limits: `.mp3`, exactly `audio/mpeg`, 1 B to 100 MiB; 5 intents/min per admin (429 + Retry-After).
   A new intent fails (`SUPERSEDED`) any unfinished earlier intent of that resource.
   Published resources must be unpublished first (409 `UNPUBLISH_REQUIRED`); non-audio → 409 `RESOURCE_TYPE_MISMATCH`.
3. Browser `PUT upload_url` with exactly `headers` and **no** Authorization header or cookies.
   The URL is on the R2 S3 API host (`<account>.r2.cloudflarestorage.com`), lives at most 15 minutes,
   signs `Content-Type`, and can only write its own private `staging/` key.
4. `POST /dawah/uploads/{upload_id}/finalize/` (no body; intent owner only, others 404).
   The server HEADs the staged object (size/type/ETag), copies it **at that ETag** to a server-only
   `media/` key, and validates that immutable copy (bounded spool, MPEG Layer III frame chain + mutagen,
   duration extracted). 200 `{upload:{id,resource_id,status,error_code,message,expires_at,finalized_at}, resource, preview_url}`
   with `status` `ready|rejected|failed|expired`. Replays return the saved outcome (also after expiry).
   Retryable: 409 `UPLOAD_IN_PROGRESS` (lease held), `UPLOAD_INCOMPLETE` (no object yet), `UPLOAD_CHANGED`
   (overwritten during copy); 503 `PROVIDER_UNAVAILABLE`.
   Rejection codes: `SIZE_MISMATCH`, `CONTENT_TYPE_MISMATCH`, `FILE_TOO_LARGE`, `EMPTY_FILE`, `INVALID_MP3`;
   failure codes: `SUPERSEDED`, `RESOURCE_TYPE_CHANGED`, `UNPUBLISH_REQUIRED`, `VALIDATION_COPY_MISSING`;
   expiry: `UPLOAD_EXPIRED` (no immutable copy was made before `expires_at`).
5. `POST .../publish/` copies the validated private object (at its ETag) to a new unique public key
   `audio/<random>/<safe-name>.mp3` with `Content-Type: audio/mpeg`, `Content-Disposition: inline; filename=...`,
   `Cache-Control: public, max-age=31536000, immutable`, then records `media_url = https://<R2_PUBLIC_DOMAIN>/<key>`.
   Failures leave the resource unpublished and record the new key for cleanup.
6. `POST .../unpublish/` hides immediately, clears `media_url`, and records the public copy for deletion
   (attempted right after commit; failures stay recorded). Shared/cached bytes cannot be recalled.
   Event unpublication hides resources through visibility only; their public copies stay until the
   resource itself is unpublished or deleted.

Resource payload additions: `storage_status` (`none|pending|ready|failed|not_applicable`), `content_type`,
`size_bytes`, `uploaded_at` (changes on every newly attached asset), `media_url` (published only),
`preview_url` (admin only, signed private GET, `R2_PREVIEW_TTL_SECONDS` capped at 900 s).
Object keys, ETags and upload URLs never appear in public payloads or logs.

## Provider setup (RELEASE-03/07 apply this; not done by code)

- Two buckets: private (staging + validated) and public (delivery). Disable r2.dev on both for production;
  attach the organization media custom domain to the public bucket only.
- Token: bucket-scoped Object Read & Write on exactly these two buckets.
- Private bucket CORS: `AllowedOrigins` = exact app origins (`R2_CORS_ALLOWED_ORIGINS`), `AllowedMethods: [PUT]`,
  `AllowedHeaders: [content-type]`, `ExposeHeaders: [ETag]`, `MaxAgeSeconds: 3600`.
- Public bucket CORS (if the player is cross-origin and reads via fetch): `GET, HEAD`, `Range` header allowed.
- Lifecycle: expire only the private `staging/` prefix (e.g. 1 day). Never a blanket bucket expiry:
  `media/` and public `audio/` objects are referenced records.
- Custom-domain response headers (`X-Content-Type-Options: nosniff`) need a Cloudflare transform rule;
  S3 object metadata cannot set them.

## Operations

`python manage.py dawah_storage_cleanup [--limit 500]` expires unfinished intents past expiry,
abandons validations stuck a day past expiry, and deletes recorded orphans that no resource
references (`StorageCleanup` rows: `pending → done | skipped`). Output is counts only. Run it on
the scheduled job runner (e.g. hourly). Legacy local `file` values are never imported or deleted.

## Configuration

Existing PLAT-02 slots only: `R2_ACCOUNT_ID` (or `R2_S3_ENDPOINT_URL`), `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, `R2_PRIVATE_BUCKET`, `R2_PUBLIC_BUCKET` (must differ), `R2_PUBLIC_DOMAIN`
(https), `R2_UPLOAD_INTENT_TTL_SECONDS` (≤ 900), `R2_PREVIEW_TTL_SECONDS` (≤ 900),
`R2_CORS_ALLOWED_ORIGINS` (bucket provisioning input). Incomplete settings disable storage (503).
Optional throttle override: `REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['dawah_upload']` (default `5/min`).

## Tests

Fake-adapter tests run in the normal suite. A live S3-compatible proof
(`dawah/tests/test_s3_integration.py`) runs only when `DAWAH_S3_TEST_ENDPOINT`, `_ACCESS_KEY`,
`_SECRET_KEY`, `_PRIVATE_BUCKET`, `_PUBLIC_BUCKET`, `_PUBLIC_BASE_URL` (optional `_REGION`) point at
throwaway buckets. Set `DAWAH_S3_TEST_EMULATOR=moto` only for a moto run: it skips (with reasons)
exactly the cases moto cannot emulate. Never set it against R2. Every case must pass on R2 (RELEASE-07).

### Emulator evidence (moto) - NOT R2 proof

moto is an AWS S3 emulator, not Cloudflare R2. Passing here shows that the adapter and API flow
work against a real S3 HTTP wire protocol (botocore signing, path-style addressing, copy, range).
It does **not** prove R2 behavior, R2 CORS, the custom domain, or signature enforcement. Docker Hub,
quay.io and dl.min.io were unreachable from the build environment, so MinIO could not be used.

Run (2026-11-10, moto 5.2.5 `moto_server` in a throwaway venv outside the project, buckets created
via boto3, public bucket given an anonymous `s3:GetObject` policy):

| Case | moto result |
|---|---|
| API end to end: intent → browser PUT with signed `Content-Type` → finalize (HEAD, copy at ETag, stream + MP3 validation) → preview GET → publish (copy to public) → public Range 206 → unpublish deletes public copy | pass |
| Presigned PUT with `Content-Type` lands an object; server-observed size/type/ETag | pass |
| Copy at the observed ETag | pass |
| Staging overwrite through the still-valid PUT URL leaves the validated copy unchanged | pass |
| Public GET `Range: bytes=0-99` → 206 with `Content-Range`, `Content-Type`, `Content-Disposition`, `Cache-Control`; adapter range read | pass |
| Admin preview (signed GET) reads the private object with the response Content-Type override | pass |
| Anonymous GET of a private-bucket object is refused | pass |
| Signed Content-Type enforced (wrong type → 403) | skipped: moto does not verify presigned signatures (default mode accepts anything; its IAM-auth mode fails query-string auth with HTTP 500) |
| Signature bound to the staging key (swapped key / unsigned PUT refused) | skipped: same reason |
| `x-amz-copy-source-if-match` refuses a stale ETag (also after a staging overwrite) | skipped: moto 5.2.5 implements only `if-none-match` for copies; the adapter's request does carry `x-amz-copy-source-if-match` (checked on the wire) |
| Anonymous public HEAD → 200 | skipped: moto authorizes HEAD as a nonexistent `s3:HeadObject` action, so an `s3:GetObject` policy still gives 403 for HEAD (confirmed by adding that action) |

The fake-adapter suite covers the skipped properties at the protocol level. The provider-level
proof for them remains RELEASE-07 on R2.
