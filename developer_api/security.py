import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings


def client_ip(request):
    """Caller IP. Behind PythonAnywhere's proxy REMOTE_ADDR is the proxy, so a trusted header can be configured."""
    header = getattr(settings, 'API_CLIENT_IP_HEADER', 'HTTP_X_REAL_IP')
    ip = request.META.get(header) or request.META.get('REMOTE_ADDR')
    return (ip or '').split(',')[0].strip() or None


def ip_allowed(ip, whitelist_text):
    """Empty whitelist = allow all. Entries may be single IPs or CIDR ranges."""
    entries = [line.strip() for line in (whitelist_text or '').replace(',', '\n').splitlines() if line.strip()]
    if not entries:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def validate_ip_whitelist(text):
    for line in (text or '').replace(',', '\n').splitlines():
        if line.strip():
            ipaddress.ip_network(line.strip(), strict=False)  # raises ValueError


def validate_webhook_url(url):
    """Webhook targets must be public HTTPS URLs (blocks SSRF to internal addresses). Raises ValueError."""
    parsed = urlparse(url)
    allow_http = getattr(settings, 'DEBUG', False)
    if parsed.scheme != 'https' and not (allow_http and parsed.scheme == 'http'):
        raise ValueError('Webhook URL must start with https://')
    if not parsed.hostname:
        raise ValueError('Webhook URL is not valid.')
    if allow_http:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError('Webhook host could not be resolved.')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError('Webhook URL must point to a public server.')
