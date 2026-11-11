from django.apps import AppConfig


class DawahConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'dawah'

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
