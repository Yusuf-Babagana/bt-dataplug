from django.apps import AppConfig


class DeveloperApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'developer_api'
    verbose_name = 'Developer API'

    def ready(self):
        # When the VTU gateway settles a purchase (webhook / poll / sweep), settle the developer order that owns it.
        from vtu_app.gateway import ledger
        from . import services
        ledger.add_listener(services.on_core_settled)
