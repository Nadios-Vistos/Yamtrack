from django.apps import AppConfig


class SosoblueConfig(AppConfig):
    """Household features kept apart from the upstream code.

    Everything specific to this deployment lives in this app, with its own
    migrations, so that merging FuzzyGrim/Yamtrack stays conflict-free.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "sosoblue"
    verbose_name = "SosoBlue"

    def ready(self):
        """Connect the signal handlers."""
        import sosoblue.signals  # noqa: F401, PLC0415
