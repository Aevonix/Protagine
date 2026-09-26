"""One Mind factory and one router list (integration map X4d).

The sidecar and the benchmark's mind arm build their Mind through ``protagine.mind.factory``:
both pass the same wiring and mount the same mind routes, so a faculty wired once reaches both.
"""

import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient

from protagine.mind import factory
from protagine.qualification import native_memory_worker as worker
from protagine.qualification import paired_worker


def _record(monkeypatch):
    calls = []
    real = factory.Mind

    def recording(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(factory, "Mind", recording)
    return calls


def _mind_paths(app):
    """The mind routes ``app`` serves, whether FastAPI copied the included routes or keeps the router."""
    wanted = {route.path for router in factory.mind_routers() for route in router.routes}

    def paths(routes):
        for route in routes:
            included = getattr(route, "original_router", None)
            if included is not None:
                yield from paths(included.routes)
            else:
                yield getattr(route, "path", None)
    return wanted & set(paths(app.routes))


def test_the_sidecar_and_the_benchmark_arm_build_the_same_mind_and_mount_the_same_routes(tmp_path, monkeypatch):
    from protagine.config import write_api_key
    from protagine.server import create_app

    calls = _record(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PROTAGINE_HOME", str(home))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(home))
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", "p-01")
    monkeypatch.delenv("PROTAGINE_API_KEY", raising=False)
    monkeypatch.setenv("PROTAGINE_EMBED_PROVIDER", "skip")   # no embedder or reranker weights are loaded
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    write_api_key("factory-key", home)
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1:7777", client=("127.0.0.1", 50000)) as client:
        assert client.get("/v1/mind/state", headers={"Authorization": "Bearer factory-key"}).status_code == 200
    assert len(calls) == 1, "the sidecar builds its Mind through build_mind"
    server_kwargs = calls.pop()

    arm = FastAPI()
    worker.mount_routes(arm, mind=True)
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path / "arm" / "memory-state"))
    with paired_worker.provider_read_services(tmp_path / "arm"):   # the arm's host stores, as ``prepare`` opens them
        with worker.serve_mind(arm, tmp_path / "arm", "p-01", worker.mind_section(True)):
            pass
    assert len(calls) == 1, "the benchmark arm builds its Mind through build_mind"
    arm_kwargs = calls.pop()

    shared = set(server_kwargs) - factory.PROCESS_OPTIONS
    assert shared == set(arm_kwargs) - factory.PROCESS_OPTIONS
    # Every reader a faculty needs is wired in both, not only named.
    for name in ("capture", "followups", "comms", "contact_affect", "packet_for", "claims_for", "appraisals"):
        assert arm_kwargs[name] is not None and server_kwargs[name] is not None, name
    everything = {route.path for router in factory.mind_routers() for route in router.routes}
    assert {"/v1/mind/narrative", "/v1/mind/consolidate", "/v1/mind/people"} <= everything
    assert _mind_paths(app) == _mind_paths(arm) == everything


def test_a_caller_cannot_wire_a_faculty_past_the_factory(tmp_path):
    import pytest

    with pytest.raises(TypeError, match="wired here"):
        factory.build_mind(object(), config={}, store=None, state_dir=tmp_path, ledger=None, owner_id=None,
                           contacts=object())


def test_no_mind_is_constructed_outside_the_factory():
    """A new faculty's wiring goes into ``mind_kwargs``; nothing else calls ``Mind(...)`` in production."""
    from protagine import server
    from protagine.qualification import native_memory_worker
    for module in (server, native_memory_worker):
        source = inspect.getsource(module)
        assert "build_mind(" in source and " Mind(" not in source and "=Mind(" not in source, module.__name__
