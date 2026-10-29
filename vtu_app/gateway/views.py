import logging

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import engine, registry

logger = logging.getLogger("vtu_app.gateway")


def _client_ip(request):
    return request.META.get("REMOTE_ADDR")


class ProviderCallbackView(APIView):
    """
    Generic provider webhook: /api/v1/vtu/webhook/<provider>/. The adapter parses and, where the provider
    signs callbacks, verifies it. An unverified callback is only a "check now" trigger: the real status is
    fetched server-to-server, so a forged callback cannot flip a transaction. Repeats are no-ops
    (ledger.settle is idempotent).
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, provider):
        return self._handle(request, provider)

    def post(self, request, provider):
        return self._handle(request, provider)

    def _handle(self, request, provider):
        adapter = registry.get_adapter(provider)
        if adapter is None or not adapter.config.enabled:
            return Response({"status": "unknown_provider"}, status=404)

        raw = request.query_params if request.method == "GET" else request.data
        logger.info("VTU_WEBHOOK received provider=%s ip=%s payload=%s", provider, _client_ip(request), str(dict(raw))[:500])
        try:
            http_status, body, outcome, detail = engine.handle_webhook(adapter, request)
        except Exception:
            logger.exception("VTU_WEBHOOK crashed provider=%s", provider)
            return Response({"status": "error"}, status=500)
        logger.info("VTU_WEBHOOK %s provider=%s detail=%s", outcome, provider, detail)
        return Response(body, status=http_status)
