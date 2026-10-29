from django.core.management.base import BaseCommand

from vtu_app.gateway import health, registry, router


class Command(BaseCommand):
    help = (
        "List configured VTU providers (secrets are never printed) and validate their .env configuration. "
        "With --test, run each enabled provider's read-only connectivity probe (auth/balance check - never "
        "places an order)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--test", action="store_true", help="Run the safe connectivity probe")
        parser.add_argument("--provider", help="Limit --test to one provider")

    def handle(self, *args, **options):
        for adapter in registry.all_adapters():
            cfg, state = adapter.config, health.get_state(adapter.name)
            flags = ["env:ON" if cfg.enabled else "env:off", "admin:ON" if state.admin_enabled else "admin:off"]
            if health.is_suspended(state):
                flags.append(f"SUSPENDED until {state.suspended_until:%H:%M}")
            self.stdout.write(
                f"{adapter.name:<12} {' '.join(flags):<28} priority={router.effective_priority(adapter, state)} "
                f"services={','.join(adapter.services)} failures={state.consecutive_failures}"
            )
            for err in cfg.errors() if cfg.enabled else []:
                self.stdout.write(self.style.ERROR(f"   config problem: {err}"))

            if options["test"] and cfg.enabled and cfg.is_valid and options["provider"] in (None, adapter.name):
                result = adapter.check_connectivity()
                line = f"   connectivity: {'OK' if result['ok'] else 'FAILED'} - {result['detail']} ({result['elapsed_ms']}ms)"
                self.stdout.write(self.style.SUCCESS(line) if result["ok"] else self.style.ERROR(line))
