"""S3/R2 adapter behavior without network access (botocore Stubber)."""
from urllib.parse import parse_qs, urlsplit

from botocore.stub import Stubber
from django.test import SimpleTestCase, override_settings

from dawah import storage
from dawah.storage import PRIVATE, PUBLIC, PreconditionFailed, StorageError, StorageUnavailable, UnconfiguredStorage
from dawah.storage.s3 import S3ObjectStorage

R2 = dict(R2_ACCOUNT_ID='acct123', R2_S3_ENDPOINT_URL='', R2_ACCESS_KEY_ID='AKIDEXAMPLE',
          R2_SECRET_ACCESS_KEY='example-secret-not-real', R2_PRIVATE_BUCKET='pyth-private',
          R2_PUBLIC_BUCKET='pyth-public', R2_PUBLIC_DOMAIN='media.example.org')


def adapter():
    return S3ObjectStorage(endpoint_url='https://acct123.r2.cloudflarestorage.com', access_key_id='AKIDEXAMPLE',
                           secret_access_key='example-secret-not-real', private_bucket='pyth-private',
                           public_bucket='pyth-public', public_base_url='https://media.example.org')


class SettingsTests(SimpleTestCase):
    def tearDown(self):
        storage.reset_default_storage()

    @override_settings(**R2)
    def test_complete_settings_build_r2_adapter(self):
        built = S3ObjectStorage.from_settings()
        self.assertIsInstance(built, S3ObjectStorage)
        self.assertEqual(built.client.meta.endpoint_url, 'https://acct123.r2.cloudflarestorage.com')
        self.assertEqual(built.client.meta.region_name, 'auto')
        self.assertEqual(built.public_url('audio/x y/k.mp3'), 'https://media.example.org/audio/x%20y/k.mp3')

    def test_incomplete_or_unsafe_settings_disable_storage(self):
        cases = [
            {'R2_SECRET_ACCESS_KEY': ''},
            {'R2_PUBLIC_DOMAIN': ''},
            {'R2_PUBLIC_DOMAIN': 'http://media.example.org'},
            {'R2_PUBLIC_DOMAIN': 'https://user:pw@media.example.org'},
            {'R2_PUBLIC_BUCKET': 'pyth-private'},
            {'R2_ACCOUNT_ID': ''},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), override_settings(**{**R2, **overrides}):
                self.assertIsNone(S3ObjectStorage.from_settings())

    @override_settings(R2_ACCESS_KEY_ID='', R2_S3_ENDPOINT_URL='')
    def test_unconfigured_default_is_unavailable(self):
        storage.reset_default_storage()
        default = storage.get_storage()
        self.assertIsInstance(default, UnconfiguredStorage)
        with self.assertRaises(StorageUnavailable):
            default.presign_put('staging/x', content_type='audio/mpeg', content_length=1, expires_in=900)


class PresignTests(SimpleTestCase):
    def test_put_signs_content_type_on_the_s3_host_only(self):
        url = adapter().presign_put('staging/audio/tok', content_type='audio/mpeg', content_length=10,
                                    expires_in=900)
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        self.assertEqual(parts.netloc, 'acct123.r2.cloudflarestorage.com')
        self.assertEqual(parts.path, '/pyth-private/staging/audio/tok')
        self.assertEqual(query['X-Amz-Expires'], ['900'])
        self.assertEqual(query['X-Amz-SignedHeaders'], ['content-length;content-type;host'])
        self.assertNotIn('media.example.org', url)
        self.assertNotIn('example-secret-not-real', url)

    def test_preview_get_is_private_and_short_lived(self):
        url = adapter().presign_get('media/audio/tok.mp3', expires_in=300, content_type='audio/mpeg',
                                    filename='k.mp3')
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).path, '/pyth-private/media/audio/tok.mp3')
        self.assertEqual(query['X-Amz-Expires'], ['300'])
        self.assertEqual(query['response-content-type'], ['audio/mpeg'])
        self.assertEqual(query['response-content-disposition'], ['inline; filename="k.mp3"'])

    def test_document_preview_is_an_attachment_with_a_safe_name(self):
        from dawah.storage.s3 import content_disposition
        url = adapter().presign_get('media/documents/tok.docx', expires_in=300, filename='a"b\r\nc.docx',
                                    content_type='application/vnd.openxmlformats-officedocument.'
                                                 'wordprocessingml.document', disposition='attachment')
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['response-content-disposition'], ['attachment; filename="abc.docx"'])
        with self.assertRaises(ValueError):
            content_disposition('form-data', 'x')


class StubbedCallTests(SimpleTestCase):
    def setUp(self):
        self.storage = adapter()
        self.stub = Stubber(self.storage.client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)

    def test_head_maps_metadata_and_missing(self):
        self.stub.add_response('head_object', {'ContentLength': 10, 'ETag': '"abc"', 'ContentType': 'audio/mpeg'},
                               {'Bucket': 'pyth-private', 'Key': 'k'})
        self.stub.add_client_error('head_object', service_error_code='404', http_status_code=404)
        info = self.storage.head('k')
        self.assertEqual((info.size, info.etag, info.content_type), (10, 'abc', 'audio/mpeg'))
        self.assertIsNone(self.storage.head('missing'))

    def test_conditional_copy_to_public_bucket(self):
        self.stub.add_response('copy_object', {'CopyObjectResult': {'ETag': '"abc"'}}, {
            'Bucket': 'pyth-public', 'Key': 'audio/t/k.mp3',
            'CopySource': {'Bucket': 'pyth-private', 'Key': 'media/audio/t.mp3'},
            'CopySourceIfMatch': '"abc"', 'MetadataDirective': 'REPLACE', 'ContentType': 'audio/mpeg',
            'ContentDisposition': 'inline; filename="k.mp3"', 'CacheControl': 'public, max-age=31536000, immutable',
        })
        self.stub.add_response('head_object', {'ContentLength': 10, 'ETag': '"abc"', 'ContentType': 'audio/mpeg'},
                               {'Bucket': 'pyth-public', 'Key': 'audio/t/k.mp3'})
        info = self.storage.copy_if_match('media/audio/t.mp3', 'audio/t/k.mp3', etag='abc', dst_bucket=PUBLIC,
                                          content_type='audio/mpeg', content_disposition='inline; filename="k.mp3"',
                                          cache_control='public, max-age=31536000, immutable')
        self.assertEqual(info.size, 10)
        self.stub.assert_no_pending_responses()

    def test_errors_are_mapped_without_provider_detail(self):
        self.stub.add_client_error('copy_object', service_error_code='PreconditionFailed', http_status_code=412)
        self.stub.add_client_error('copy_object', service_error_code='InternalError', http_status_code=500,
                                   service_message='https://acct123.r2.cloudflarestorage.com/secret?X-Amz-Signature=1')
        with self.assertRaises(PreconditionFailed):
            self.storage.copy_if_match('a', 'b', etag='x', content_type='audio/mpeg')
        with self.assertRaises(StorageError) as caught:
            self.storage.copy_if_match('a', 'b', etag='x', content_type='audio/mpeg')
        self.assertEqual(str(caught.exception), 'PROVIDER_ERROR')
        self.assertIsNone(caught.exception.__cause__)

    def test_delete_is_idempotent(self):
        self.stub.add_client_error('delete_object', service_error_code='NoSuchKey', http_status_code=404)
        self.storage.delete('gone', bucket=PRIVATE)

    def test_range_read(self):
        from io import BytesIO

        from botocore.response import StreamingBody
        self.stub.add_response('get_object', {'Body': StreamingBody(BytesIO(b'0123'), 4)},
                               {'Bucket': 'pyth-public', 'Key': 'k', 'Range': 'bytes=0-3'})
        stream = self.storage.open_stream('k', bucket=PUBLIC, byte_range=(0, 3))
        self.assertEqual(stream.read(), b'0123')
        stream.close()
