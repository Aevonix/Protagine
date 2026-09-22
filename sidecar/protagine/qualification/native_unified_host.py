"""Owned native gateway and canonical stores for synthetic shared-work cases."""
import asyncio
from contextlib import contextmanager
import threading


@contextmanager
def host_state(app, state, inputs, config):
    from protagine.api.routers import commitment_work, executions, host, initiative_work
    from protagine.commitments.store import CommitmentStore
    from protagine.initiatives.store import InitiativeStore
    app.include_router(executions.router)
    app.include_router(commitment_work.router)
    app.include_router(initiative_work.router)
    previous = host._commitment_store
    previous_initiatives = host._initiative_store
    store = CommitmentStore(state/'memory-state'/'commitments.db')
    initiatives = InitiativeStore(state/'memory-state')
    host.set_commitment_store(store)
    host.set_initiative_store(initiatives)
    config.setdefault('plugins', {}).setdefault('protagine', {}).update(
        execution_registry_enabled=True,
        native_tasks={'enabled': True, 'state_path': str(state/'native-tasks.sqlite3')})
    config['platforms'] = {'protagine_task': {'enabled': True}}
    config['platform_toolsets'] = {'protagine_task': []}
    config.setdefault('agent', {}).update(max_turns=4, restart_drain_timeout=5)
    config.setdefault('gateway', {}).update(loop_watchdog=False)
    try:
        yield store
    finally:
        host.set_commitment_store(previous)
        host.set_initiative_store(previous_initiatives)
        initiatives.close()


@contextmanager
def native_gateway(state):
    """Run only the installed in-process task platform in this isolated home."""
    ready = threading.Event()
    owned = {}

    def thread_main():
        async def lifetime():
            from gateway.config import GatewayConfig, Platform, PlatformConfig
            from gateway.run import GatewayRunner
            gateway = GatewayRunner(GatewayConfig(
                platforms={Platform('protagine_task'): PlatformConfig(enabled=True,
                    gateway_restart_notification=False, typing_indicator=False)},
                sessions_dir=state/'sessions', stt_enabled=False, loop_watchdog=False))
            owned.update(gateway=gateway, loop=asyncio.get_running_loop(), stop=asyncio.Event())
            try:
                if not await gateway.start():
                    raise RuntimeError('Isolated native task gateway did not connect')
                ready.set()
                await owned['stop'].wait()
            finally:
                await gateway.stop()
        try:
            asyncio.run(lifetime())
        except BaseException as exc:
            owned['error'] = exc
            ready.set()

    thread = threading.Thread(target=thread_main, name='qualification-native-gateway', daemon=True)
    thread.start()
    try:
        if not ready.wait(20) or owned.get('error'):
            raise RuntimeError('Isolated native gateway setup failed') from owned.get('error')
        yield owned['gateway']
    finally:
        if 'loop' in owned and not owned['loop'].is_closed():
            owned['loop'].call_soon_threadsafe(owned['stop'].set)
        thread.join(25)
        if thread.is_alive():
            raise RuntimeError('Isolated native gateway cleanup is unconfirmed')
        if owned.get('error'):
            raise RuntimeError('Isolated native gateway failed') from owned['error']
