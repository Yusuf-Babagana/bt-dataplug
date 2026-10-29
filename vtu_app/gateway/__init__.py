"""
Provider-agnostic VTU gateway.

    views -> engine.execute_purchase() -> router -> ProviderAdapter (one per provider)

Provider-specific request/response/status handling lives only in
``vtu/gateway/adapters/``. See docs/VTU_PROVIDERS.md.
"""
