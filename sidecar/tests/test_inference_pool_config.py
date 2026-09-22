import copy

import pytest

from protagine.inference_pool.config import parse_config


def profile():
    return {
        "version": 1,
        "endpoints": {
            "first": {
                "base_url": "http://127.0.0.1:9001/v1",
                "model": "arbitrary-model-revision",
                "max_requests": 4,
                "max_tokens": 100_000,
                "context_tokens": 32_000,
                "reservations": {"interactive": {"requests": 1, "tokens": 20_000}},
                "max_input_tokens": {"background": 4000},
            }
        },
        "routes": {
            "chat": {
                "model": "my-agent",
                "endpoints": ["first"],
                "traffic_class": "interactive",
            },
            "work": {
                "model": "my-agent",
                "endpoints": ["first"],
                "traffic_class": "background",
            },
        },
    }


def test_single_replica_shared_by_multiple_roles_and_changed_model():
    raw = profile()
    config = parse_config(raw)
    assert config.routes["chat"].endpoints == config.routes["work"].endpoints
    raw["endpoints"]["first"]["model"] = "another-family"
    raw["endpoints"]["second"] = dict(
        raw["endpoints"]["first"], base_url="https://replica.invalid:9443/v1"
    )
    raw["routes"]["chat"]["endpoints"].append("second")
    config = parse_config(raw)
    assert len(config.endpoints) == 2
    assert config.endpoints["second"].model == "another-family"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x["endpoints"]["first"].update(max_requests=True),
        lambda x: x["endpoints"]["first"].update(max_tokens=-1),
        lambda x: x["endpoints"]["first"].update(
            base_url="http://user:secret@localhost:9001/v1"
        ),
        lambda x: x["endpoints"]["first"].update(api_key="accidental-secret"),
        lambda x: x["routes"]["chat"].update(endpoints=["missing"]),
        lambda x: x["routes"]["chat"].update(endpoints=["first", "first"]),
        lambda x: x["routes"]["chat"].update(default_output_tokens=99_999),
        lambda x: x.update(request_timeout_seconds=float("nan")),
        lambda x: x["endpoints"]["first"]["reservations"]["interactive"].update(
            requests=5
        ),
        lambda x: x["endpoints"]["first"]["reservations"]["interactive"].update(
            tokens=100_001
        ),
        lambda x: x["endpoints"]["first"]["reservations"].update(typo={"requests": 1}),
    ],
)
def test_invalid_policy_rejected(mutation):
    raw = profile()
    mutation(raw)
    with pytest.raises(ValueError):
        parse_config(raw)


def test_duplicate_physical_backend_does_not_double_capacity():
    raw = profile()
    raw["endpoints"]["alias"] = copy.deepcopy(raw["endpoints"]["first"])
    raw["endpoints"]["alias"]["base_url"] = "http://127.0.0.1:9001/"
    with pytest.raises(ValueError, match="duplicate physical endpoint"):
        parse_config(raw)


def test_unknown_fields_and_unused_classes_are_errors():
    raw = profile()
    raw["routes"]["chat"]["queue_timout_seconds"] = 1
    with pytest.raises(ValueError, match="unknown fields"):
        parse_config(raw)
