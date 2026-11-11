# Da'wah durable media: upload, validation and delivery (DAWAH-02, DAWAH-07)

Authority: D2 "R2 upload validation and delivery", D0 A04, D5 R2 settings.
Code: `dawah/storage/` (port + S3/R2 adapter + in-memory double),
`dawah/uploads.py` (intent/finalize/promotion/cleanup), `dawah/media.py`
(MP3 validation), `dawah/documents.py` + `dawah/images.py` (DAWAH-07 PDF/DOCX/JPEG/PNG validation),
`dawah/management/commands/dawah_storage_cleanup.py`. Documents and featured images: see the DAWAH-07
section at the end.

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
   signs `Content-Type` **and `Content-Length`** (the declared `size_bytes`), and can only write its
   own private `staging/` key. A body of another size or type is refused by storage (403).
4. `POST /dawah/uploads/{upload_id}/finalize/` (no body; intent owner only, others 404).
   The server HEADs the staged object (size/type/ETag), copies it **at that ETag** to a server-only
   `media/` key, and validates that immutable copy, read with `If-Match` on the copy's ETag (bounded
   spool, MPEG Layer III frame chain, then mutagen `MPEGInfo` for the duration). ID3 tags are never
   parsed (compressed frames are a decompression bomb); a leading tag is skipped by its header length
   and one declaring more than 16 MiB is `INVALID_MP3`. Any unexpected parser error is `INVALID_MP3`;
   any unexpected internal failure (e.g. temp disk full) releases the lease and returns a sanitized 503. 200 `{upload:{id,resource_id,status,error_code,message,expires_at,finalized_at}, resource, preview_url}`
   with `status` `ready|rejected|failed|expired`. Replays return the saved outcome (also after expiry).
   Retryable: 409 `UPLOAD_IN_PROGRESS` (lease held), `UPLOAD_INCOMPLETE` (no object yet), `UPLOAD_CHANGED`
   (overwritten during copy), `UPLOAD_LEASE_EXPIRED` (validation outlived its 300 s lease; nothing is
   attached); 503 `PROVIDER_UNAVAILABLE`. After a failure the lease is released: with no immutable copy
   yet the intent returns to `issued` (normal expiry); with one it stays `validating` (no lease) so a
   retry re-validates the same copy, and the cleanup command abandons it one day after expiry.
   Rejection codes: `SIZE_MISMATCH`, `CONTENT_TYPE_MISMATCH`, `FILE_TOO_LARGE`, `EMPTY_FILE`, `INVALID_MP3`;
   failure codes: `SUPERSEDED`, `RESOURCE_TYPE_CHANGED`, `UNPUBLISH_REQUIRED`, `VALIDATION_COPY_MISSING`;
   expiry: `UPLOAD_EXPIRED` (no immutable copy was made before `expires_at`).
5. `POST .../publish/` copies the validated private object (at its ETag) to a new unique public key
   `audio/<random>/<safe-name>.mp3` with `Content-Type: audio/mpeg`, `Content-Disposition: inline; filename=...`,
   `Cache-Control: public, max-age=31536000, immutable`, then records `media_url = https://<R2_PUBLIC_DOMAIN>/<key>`.
   The new key's cleanup row is recorded **before** the copy with a one-hour grace (`not_before`);
   it is `cancelled` when attached and made immediately eligible on any failure, which leaves the
   resource unpublished.
6. `POST .../unpublish/` hides immediately, clears `media_url`, and records the public copy for deletion
   and CDN purge (attempted right after commit; failures stay recorded). Already downloaded bytes
   cannot be recalled.
7. Event unpublication keeps its resources' publication status (D2) but retires every public copy
   the same way (deleted, purged, `media_url` cleared). Event re-publication re-promotes each ready
   published resource to a **new** public key, all or nothing: if any copy fails the event stays
   unpublished (503) and the copies already made are cleaned up.

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
  `media/` and public `audio/`, `documents/`, `images/` objects are referenced records.
- Custom-domain response headers (`X-Content-Type-Options: nosniff`) need a Cloudflare transform rule;
  S3 object metadata cannot set them.

## Operations

`python manage.py dawah_storage_cleanup [--limit 500]` expires unfinished intents past expiry
(including lease-less `validating` rows), abandons validations stuck a day past expiry, and deletes
recorded orphans past their `not_before` that no resource or unfinished intent references
(`StorageCleanup` rows: `pending → done | skipped`; promotion rows `cancelled` once attached).
For public objects it then purges the recorded URL through the cache-purge port; a failed purge
keeps the row pending (`last_error_code` `PURGE_*`) for the next run. Output is counts only.
Deleting an admin keeps their intents (`created_by` becomes null): they can no longer be finalized
and are expired/cleaned by this command. Run it on
the scheduled job runner (e.g. hourly). Legacy local `file` values are never imported or deleted.

## Configuration

PLAT-02 slots: `R2_ACCOUNT_ID` (or `R2_S3_ENDPOINT_URL`, which must be https), `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`, `R2_PRIVATE_BUCKET`, `R2_PUBLIC_BUCKET` (must differ), `R2_PUBLIC_DOMAIN`
(https custom domain; `*.r2.dev` is refused), `R2_UPLOAD_INTENT_TTL_SECONDS` (≤ 900),
`R2_PREVIEW_TTL_SECONDS` (≤ 900), `R2_CORS_ALLOWED_ORIGINS` (bucket provisioning input).
Incomplete or unsafe settings disable storage (503). Wrong bucket/credentials at runtime
(`NoSuchBucket`, `AccessDenied`/403, invalid key/signature) map to 503, never to "missing object".
This relies on R2 answering a HEAD of a missing key with 404 (it does for Object Read & Write tokens);
a token without list permission on AWS S3 would see 403 instead.

Added by DAWAH-02 (minimal `pythagoras/settings.py` lines):

- `R2_ALLOW_R2_DEV_DOMAIN` (default false): allows an `*.r2.dev` public domain only when `DEBUG` is
  also true. Development only.
- `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_PURGE_API_TOKEN`: both set (32-hex zone ID of the media domain;
  token with only Zone > Cache Purge) enables Cloudflare purge-by-URL (30 URLs/request, 10 s timeout).
  Either missing: no-op purger. Backend-only secret; never `VITE_`.
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
via boto3, public bucket given an anonymous `s3:GetObject` policy; re-run after the review hardening
with signed Content-Length: 12 cases, 7 pass, 5 skipped under `DAWAH_S3_TEST_EMULATOR=moto`; without
the flag exactly those 5 fail):

| Case | moto result |
|---|---|
| API end to end: intent → browser PUT with signed `Content-Type` → finalize (HEAD, copy at ETag, stream + MP3 validation) → preview GET → publish (copy to public) → public Range 206 → unpublish deletes public copy | pass |
| Presigned PUT with `Content-Type` lands an object; server-observed size/type/ETag | pass |
| Copy at the observed ETag | pass |
| Staging overwrite through the still-valid PUT URL leaves the validated copy unchanged | pass |
| Public GET `Range: bytes=0-99` → 206 with `Content-Range`, `Content-Type`, `Content-Disposition`, `Cache-Control`; adapter range read | pass |
| Admin preview (signed GET) reads the private object with the response Content-Type override | pass |
| Anonymous GET of a private-bucket object is refused | pass |
| Signed Content-Type / Content-Length enforced (wrong type or size → 403) | skipped: moto does not verify presigned signatures (default mode accepts anything; its IAM-auth mode fails query-string auth with HTTP 500) |
| Signature bound to the staging key (swapped key / unsigned PUT refused) | skipped: same reason |
| `x-amz-copy-source-if-match` refuses a stale ETag (also after a staging overwrite) | skipped: moto 5.2.5 implements only `if-none-match` for copies; the adapter's request does carry `x-amz-copy-source-if-match` (checked on the wire) |
| Anonymous public HEAD → 200 | skipped: moto authorizes HEAD as a nonexistent `s3:HeadObject` action, so an `s3:GetObject` policy still gives 403 for HEAD (confirmed by adding that action) |

The fake-adapter suite covers the skipped properties at the protocol level. The provider-level
proof for them remains RELEASE-07 on R2.

## YouTube video links (DAWAH-05)

Video resources reference an existing YouTube video; nothing is uploaded and no
YouTube API/credential/network call is made. `youtube_url` is accepted on
resource create/PATCH (admin), parsed structurally (`dawah/youtube.py`) and only
the 11-character video ID is stored (`youtube_video_id`). The payload exposes
`playback` = `{provider, video_id, watch_url, embed_url}` built server-side
(`https://www.youtube.com/watch?v=<id>`, `https://www.youtube-nocookie.com/embed/<id>`)
(null without a valid ID).
Accepted: HTTPS on youtube.com, www./m.youtube.com (`/watch?v=`, `/embed/`,
`/shorts/`), youtu.be/`<id>`, www.youtube-nocookie.com/embed/`<id>`; other query
parameters and fragments are discarded. A video is publishable/visible only once
it has an ID. Changing the link of a published video is `409 UNPUBLISH_REQUIRED`.
A valid link does not guarantee the video exists or allows embedding.

## Event documents and featured images (DAWAH-07)

Same storage port, intent → direct PUT → finalize protocol, lease/replay rules, promotion and recorded
cleanup as audio. `UploadIntent.kind` is `audio | document | image`; audio/document intents target a
resource, image intents target the event (`UploadIntent.event`; a check constraint enforces exactly one).

| Route (admin only) | Body | Accepted | Limit |
|---|---|---|---|
| `POST /dawah/events/{e}/resources/{id}/document/` | `{file_name, content_type, size_bytes}` | `application/pdf` (`.pdf`), `application/vnd.openxmlformats-officedocument.wordprocessingml.document` (`.docx`) | 20 MiB |
| `POST /dawah/events/{id}/featured-image/` | same | `image/jpeg` (`.jpg`/`.jpeg`), `image/png` (`.png`) | 10 MiB |
| `POST /dawah/uploads/{upload_id}/finalize/` | none | same route for every kind | |

Intent 201 data: `{upload_id, resource_id (null for images), event_id, method:"PUT", upload_url,
headers:{Content-Type}, expires_at, max_size_bytes}`. The file-name extension must match the declared type.
The 5/min per-admin throttle (`dawah_upload`) is shared by audio, document and image intents. Finalize has its
own per-admin throttle, 30/min (`dawah_finalize`, override via `DEFAULT_THROTTLE_RATES`), bounding concurrent
validation work; 429 `THROTTLED` with `Retry-After`.
Document intents: 409 `RESOURCE_TYPE_MISMATCH` (not a `doc` resource), `UNPUBLISH_REQUIRED` (published
documents cannot be replaced). Image intents are allowed in any event state.

Finalize 200 data: `{upload:{id, kind, resource_id, event_id, status, error_code, message, expires_at,
finalized_at}, resource, event, preview_url}`; `resource` is set for audio/documents (and `event` null), `event`
(full event payload) for images (and `resource` null). Additional retryable 409: `MEDIA_CHANGED` (the event
was published while its image was validated; retry re-validates the kept copy and promotes it).

Validation of the immutable copy (`dawah/documents.py`, `dawah/images.py`, never on staging bytes):

- **PDF** (byte level; page content streams are never decoded): `%PDF-1.0`..`2.0` header at offset 0, `%%EOF` in the last 1 KiB, a
  final `startxref` offset inside the file pointing at an `xref` table with a `/Root` trailer or an
  `/Type /XRef` stream with `/Root`, and at least one `obj`/`endobj`. → `INVALID_PDF`. Active content is refused
  with `PDF_ACTIVE_CONTENT`: the names `/JavaScript`, `/JS`, `/Launch`, `/EmbeddedFile(s)`, `/RichMedia` (also
  `#xx` hex-escaped), anywhere in the file and inside compressed object streams (`/Type /ObjStm`, FlateDecode only,
  inflated in 64 KiB steps under a 64 MiB total cap; a non-Flate or over-cap object stream is `INVALID_PDF`).
  `/OpenAction`/`/AA` with a JavaScript action are refused through the action itself; a plain GoTo open action is
  accepted. Page content streams are not inflated (they cannot hold action dictionaries). Nothing is executed or
  rendered server-side.
- **DOCX** (stdlib `zipfile`, nothing extracted to disk): entry count ≤ 1000 read from the end-of-central-directory
  record *before* the directory is parsed; no ZIP64/multi-disk/encrypted entries; only stored/deflated; safe
  entry names; per-entry uncompressed ≤ 50 MiB, total ≤ 60 MiB, ratio ≤ 100:1 for entries over 1 MiB and
  ≤ 50:1 for the whole archive once it exceeds 4 MiB uncompressed (`DOCX_LIMITS_EXCEEDED`); every entry is decompressed in 256 KiB chunks, never past its declared size, with
  the CRC recomputed (`INVALID_DOCX` on any mismatch). Requires `[Content_Types].xml` and `word/document.xml`
  with the plain `wordprocessingml.document.main+xml` type (templates and other OOXML kinds are `INVALID_DOCX`).
  `vbaProject.bin`, `vbaData.xml`, `activeX/` parts and macro-enabled/VBA content types are
  `DOCX_MACROS_NOT_ALLOWED`; external relationships other than hyperlinks (templates, OLE objects, linked
  images, frames, sub-documents) are `DOCX_EXTERNAL_CONTENT`. Parsed XML parts are ≤ 4 MiB and may not
  contain a DTD/entity declaration.
- **Images** (Pillow, restricted to the one declared format): magic bytes must match the declared type
  (`CONTENT_TYPE_MISMATCH`), dimensions ≤ 12000 px per side and ≤ 16 megapixels checked from the header before
  any decoding (`IMAGE_TOO_LARGE`; Pillow's own bomb warning is escalated too), PNG chunk CRCs verified, then one
  full decode (`INVALID_IMAGE` on truncation/corruption/other formats such as GIF/SVG).
- **Featured images are never published as uploaded.** After the decode a sanitized derivative is re-encoded
  server-side: EXIF orientation applied; EXIF (incl. GPS), XMP, IPTC, comments and PNG text/`eXIf`/`tIME` chunks
  dropped; first frame only (APNG/MPO become a still); palette/16-bit/tRNS PNGs become RGBA, CMYK JPEGs become
  RGB; the ICC profile is kept (≤ 1 MiB; dropped for CMYK); long side at most 4096 px (JPEGs larger than that are
  decoded at a reduced DCT scale with `draft`, then resampled); JPEG quality 88, same MIME type as declared.
  The derivative is written with the new port method `put_object` to a new server-only key
  `media/images/<random>.jpg|.png` (cleanup row `derivative_pending` recorded first, cancelled on attach) and
  becomes the event's private object (the payload's size and the preview are the derivative's); the raw
  validation copy is recorded for deletion (`image_sanitized`). Promotion copies only the derivative. A failed
  derivative write is a retryable 503 (its `derivative_failed` row is made eligible).
- **Memory bound per image finalize:** decoded pixels ≤ 16 MP × 4 bytes = 64 MiB (a JPEG drafted to the output
  scale uses far less), at most one transformed copy of output size (≤ 4096² × 4 bytes = 64 MiB), the encoded
  derivative (capped at 24 MiB, else `IMAGE_TOO_LARGE`) and the spooled input (8 MiB in memory, rest on temp
  disk): about 160 MiB worst case. Documents: the spooled file (≤ 20 MiB, read whole for the PDF scan), at most
  4 MiB per parsed DOCX XML part, 256 KiB / 64 KiB decompression chunks. With 30 finalizes/min per admin, size
  worker memory for the number of concurrent admins.
- A PDF uploaded as DOCX (or vice versa) is `CONTENT_TYPE_MISMATCH`. Shared codes: `SIZE_MISMATCH`,
  `FILE_TOO_LARGE`, `EMPTY_FILE`, plus the DAWAH-02 failure/expiry codes.

Delivery (public copies, `Cache-Control: public, max-age=31536000, immutable`, ASCII-safe quoted file name):

| Kind | Private key | Public key | Content-Disposition (public and admin preview) |
|---|---|---|---|
| document | `media/documents/<random>.pdf\|.docx` | `documents/<random>/<safe-name>` | `attachment` (PDF too: documents are never rendered inline from the media domain) |
| image | `media/images/<random>.jpg\|.png` | `images/<random>/<safe-name>` | `inline` |

Payloads:

- Resources (all types) add `file_name` (validated asset only, else null). A document exposes `type=doc`,
  `content_type`, `size_bytes`, `file_name`, `uploaded_at` and `media_url` (published only). Archive `type=doc`
  filters documents. A document is publishable once ready (409 `MEDIA_NOT_READY` otherwise).
- Events add `featured_image_url` (public URL only while the event is published and has a promoted image),
  `featured_image_preview_url` (admin readers only, short-lived signed GET, any event state),
  `featured_image_storage_status` (`none|pending|ready|failed`), `featured_image_uploaded_at`,
  `featured_image_alt` (writable on event POST/PUT/PATCH: plain text ≤ 250 characters, trimmed; markup `<`/`>`
  and control characters are refused). The nested `event` of every resource payload adds `featured_image_url`
  and `featured_image_alt`.
- There is no endpoint to remove a featured image (accepted for Phase 1); an admin replaces it with a new upload.

Featured-image lifecycle:

- Draft/unpublished event: finalize attaches the validated private copy; nothing is public.
- Published event: finalize promotes the new copy to a new public key first (cleanup row recorded before the
  copy, one-hour grace), then, under the event lock, swaps the pointer and records the previous public copy
  (deleted and CDN-purged after commit) and the previous private copy. A failed promotion is 503 with the old
  image still visible; the validated copy is kept and finalize can be retried. A rejected replacement leaves the
  current image untouched. If the event is unpublished between promotion and attach, the image attaches
  privately and the unused public copy is made eligible for cleanup (`promotion_unused`).
- Event unpublish retires the public image copy (deleted + purged, URL cleared); event (re)publish promotes the
  ready image to a new key with the resources, all or nothing (`MEDIA_CHANGED`/`MEDIA_NOT_READY`/503 leave the
  event unpublished).
- Deleting a resource referenced by another domain (e.g. ASSESS-07 `quiz.ModuleResource`, PROTECT) is 409
  `RESOURCE_REFERENCED` with `fields.references = {"<app.model>": [ids]}` (generic reverse-relation check, plus
  `ProtectedError`/`RestrictedError` handling for races). Event delete reports its resources' references the
  same way. Nothing is deleted or recorded for cleanup in that case.
- Event delete records the image's private/public copies and its image intents' keys (`event_deleted`).
  `StorageCleanup.event_id` identifies featured-image rows; the cleanup command never deletes a key an event
  or resource still references.

Reuse by other domains (D3 Arabic modules, ASSESS-07): `dawah.public.visible_resources(ids)`,
`visible_resource_ids(ids)` and `public_resource_payloads(resources)` return only currently visible
resources in exactly the anonymous `GET /dawah/resources/{id}/` shape (no preview URLs or admin fields).
Module authorization stays with the caller and cannot widen visibility.

Migration `0006_event_documents_and_featured_images`: adds the event `featured_image_*` columns (status
`none`), `UploadIntent.event` + kind choices + nullable `resource` + the single-target check constraint, and
`StorageCleanup.event_id`. Existing rows are unchanged; the legacy `featured_image` ImageField and legacy
resource `file` values are preserved, never served, and never mark anything ready or published. No new
settings or environment variables.

Live proof: `dawah/tests/test_s3_integration.py::LiveDocumentAndImageFlowTests` (same `DAWAH_S3_TEST_*`
variables). Emulator runs 2026-11-11 (initial, and again after review with the derivative flow) (moto 5.2.5, throwaway venv outside the project): 14 cases, 9 pass
(including both DAWAH-07 flows: DOCX intent → PUT → finalize → attachment preview → publish → public GET with
`attachment` disposition → unpublish delete; PNG featured image on a published event, replaced once, old public
copy gone), 5 skipped as the documented moto limits above. Not R2 proof; RELEASE-07 repeats it on R2.
