"""The channel manifest carries no delivery webhook.

Earlier releases accepted ``delivery_webhook`` and validated it, but nothing
ever sent to it. The key is now ignored so old channels keep registering.
"""

from protagine.channels.manifest import ChannelManifest


def _payload(**extra):
    return {
        "channel_key": "legacy-channel",
        "display_name": "Legacy",
        "gateway_family": "test",
        **extra,
    }


def test_old_delivery_webhook_key_is_ignored():
    manifest = ChannelManifest(**_payload(delivery_webhook="http://127.0.0.1/hook"))
    assert manifest.channel_key == "legacy-channel"
    assert "delivery_webhook" not in manifest.model_dump()


def test_router_has_no_webhook_validator():
    from protagine.channels import router

    assert not hasattr(router, "_validate_webhook")
    assert not hasattr(router, "_PRIVATE_NETWORKS")
