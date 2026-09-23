"""Protagine delivery helpers.

The proactive delivery bridge and its ``/internal/deliver`` path are gone:
owner messages go through the mind's outbox and the plugin sends them
verbatim (architecture 6.3). What is left here:

- DeliveryRateLimiter: per-person rate limiting (quiet hours, cooldown)
- ChannelRegistry: the registered channels
"""

from protagine.delivery.rate_limiter import DeliveryRateLimiter
from protagine.delivery.channels import ChannelRegistry

__all__ = ["DeliveryRateLimiter", "ChannelRegistry"]
