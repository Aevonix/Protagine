import asyncio
import json
from types import SimpleNamespace

from protagine.qualification.serving_observer import install, observe_frame


def empty():
    return {'returned_models': set(), 'usage': {}, 'finish_reason': None,
        'first_reasoning_at': None, 'first_content_at': None, 'first_delta_at': None,
        'last_delta_at': None, 'content_characters': 0, 'content_prefix': ''}


def test_reasoning_is_not_first_content_and_usage_is_not_requested_length():
    row = empty()
    observe_frame({'model': 'fixture', 'choices': [{'delta': {'reasoning': 'private analysis'}}]}, row, 10)
    observe_frame({'choices': [{'delta': {'content': 'Final answer'}, 'finish_reason': 'stop'}]}, row, 14)
    observe_frame({'choices': [], 'usage': {'completion_tokens': 27, 'prompt_tokens': 120,
        'completion_tokens_details': {'reasoning_tokens': 22}}}, row, 15)
    assert row['first_delta_at'] == row['first_reasoning_at'] == 10
    assert row['first_content_at'] == row['last_delta_at'] == 14
    assert row['usage'] == {'completion_tokens': 27, 'prompt_tokens': 120, 'reasoning_tokens': 22}
    assert row['finish_reason'] == 'stop'


def test_concurrent_observations_do_not_mix_requests_or_retain_text():
    client = SimpleNamespace(json=json)
    async def original(inputs, pbar=None):
        import time
        began = time.perf_counter()
        await asyncio.sleep(0)
        client.json.loads(json.dumps({'model': inputs.model, 'choices': [{'delta': {'content': inputs.model}}]}))
        return SimpleNamespace(start_time=began, latency=.01, success=True)
    client.ASYNC_REQUEST_FUNCS = {'vllm-chat': original}
    output, answers = [], []
    install(client, output.append, answer_sink=answers.append)
    async def main():
        await asyncio.gather(*(client.ASYNC_REQUEST_FUNCS['vllm-chat'](
            request_func_input=SimpleNamespace(model=name, prompt='synthetic', output_len=100),
            pbar=None) for name in ('a', 'b')))
    asyncio.run(main())
    assert {row['candidate_model'] for row in output} == {'a', 'b'}
    assert max(row['request_started_monotonic_s'] for row in output) < min(
        row['request_finished_monotonic_s'] for row in output)
    for row in output:
        assert row['returned_models'] == [row['candidate_model']]
        assert row['usage_missing'] is True and row['server_usage'] == {}
        assert 'content_prefix' not in row
    assert client.json.dumps({'ordinary': True}) == json.dumps({'ordinary': True})
    assert {row['content'] for row in answers} == {'a', 'b'}
    assert all(row['content_truncated'] is False for row in answers)
    assert all('content' not in row for row in output)


def test_serial_request_spans_do_not_claim_concurrent_work():
    client = SimpleNamespace(json=json)
    async def original(inputs, pbar=None):
        import time
        return SimpleNamespace(start_time=time.perf_counter(), latency=0, success=True)
    client.ASYNC_REQUEST_FUNCS = {'vllm-chat': original}
    rows = []
    install(client, rows.append)
    async def main():
        for _ in range(2):
            await client.ASYNC_REQUEST_FUNCS['vllm-chat'](
                SimpleNamespace(model='fixture', prompt='synthetic', output_len=1))
    asyncio.run(main())
    assert rows[0]['request_finished_monotonic_s'] <= rows[1]['request_started_monotonic_s']
    assert all(row['request_finished_monotonic_s'] >= row['request_started_monotonic_s'] for row in rows)
