"""Test runner that guarantees tests never reach a real email provider."""
from django.conf import settings
from django.test.runner import DiscoverRunner


class IsolatedTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        settings.EMAIL_TRANSPORT = 'memory'
        settings.RESEND_API_KEY = ''
