from django.apps import AppConfig


class VtuAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'vtu_app'

    def ready(self):
        import vtu_app.signals
        from django.core import checks
        checks.register(check_provider_configuration, checks.Tags.compatibility)


def check_provider_configuration(app_configs, **kwargs):
    """Surface .env mistakes for any VTU provider that is switched on but missing settings."""
    from django.core import checks
    from vtu_app.gateway import registry

    return [
        checks.Warning(problem, hint="Set it in .env (see .env.example) or disable the provider.", id="vtu_app.W001")
        for problem in registry.validate_configuration()
    ]
