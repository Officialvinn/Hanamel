"""Feature flags driven by environment, so half-configured integrations
stay invisible to staff."""
import os

from django import template

register = template.Library()


@register.simple_tag
def mpesa_enabled():
    """True once Daraja credentials exist. Until then the M-Pesa page is
    reachable by URL but isn't advertised in the nav."""
    return bool(os.environ.get("MPESA_CONSUMER_KEY", "").strip())