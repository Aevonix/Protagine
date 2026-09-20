"""Supplement one pinned upstream serving client with content/usage observations.

Requests, responses, tokenization and upstream scoring stay unchanged. The added
request deadline is explicit. The timing receipt contains no prompt, generated
content or reasoning. Optional answer receipts contain bounded final-channel
content for private semantic grading. Neither receipt is a public export.
"""
import argparse
import asyncio
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import sys
import time

CLIENT_SHA256 = '26a4458f64210916716606c50c9d75578c486bcc36ce4f6c89e109fde00e79c3'
CLIENT_COMMIT = 'e087e662ba1ac4ef7747537e2a9141085efd4561'
VERSION = 'sglang-content-observation-2'
_active = ContextVar('serving_observation', default=None)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def observe_frame(frame, observation, now):
    if not isinstance(frame, dict):
        return
    if isinstance(frame.get('model'), str):
        observation['returned_models'].add(frame['model'])
    usage = frame.get('usage')
    if isinstance(usage, dict):
        for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if (count := _count(usage.get(name))) is not None:
                observation['usage'][name] = count
        details = usage.get('completion_tokens_details')
        if isinstance(details, dict) and (count := _count(details.get('reasoning_tokens'))) is not None:
            observation['usage']['reasoning_tokens'] = count
    choices = frame.get('choices') or []
    if not choices or not isinstance(choices[0], dict):
        return
    choice = choices[0]
    if isinstance(choice.get('finish_reason'), str):
        observation['finish_reason'] = choice['finish_reason']
    delta = choice.get('delta') or choice.get('message') or {}
    if not isinstance(delta, dict):
        return
    reasoning = delta.get('reasoning_content') or delta.get('reasoning')
    content = delta.get('content')
    for value, field in ((reasoning, 'first_reasoning_at'), (content, 'first_content_at')):
        if isinstance(value, str) and value:
            if observation[field] is None:
                observation[field] = now
            if observation['first_delta_at'] is None:
                observation['first_delta_at'] = now
            observation['last_delta_at'] = now
    if isinstance(content, str):
        observation['content_characters'] += len(content)
        observation['content_prefix'] = (observation['content_prefix'] + content)[:256]
        if 'answer_content' in observation:
            observation['answer_content'] = (observation['answer_content'] + content)[:65536]


def install(client, sink, *, request_deadline_seconds=300, answer_sink=None):
    """Instrument this owned client module, without changing global json behavior."""
    if not 1 <= request_deadline_seconds <= 1800:
        raise ValueError('Declare a request deadline between 1 and 1800 seconds')
    original_json = client.json
    original_request = client.ASYNC_REQUEST_FUNCS['vllm-chat']

    class ObservedJSON:
        def __getattr__(self, key):
            return getattr(original_json, key)

        def loads(self, *args, **kwargs):
            value = original_json.loads(*args, **kwargs)
            if (observation := _active.get()) is not None:
                observe_frame(value, observation, time.perf_counter())
            return value

    async def request(inputs, pbar=None):
        began = time.perf_counter()
        observation = {'returned_models': set(), 'usage': {}, 'finish_reason': None,
            'first_reasoning_at': None, 'first_content_at': None, 'first_delta_at': None,
            'last_delta_at': None, 'content_characters': 0, 'content_prefix': ''}
        if answer_sink is not None:
            observation['answer_content'] = ''
        token = _active.set(observation)
        timeout = False
        try:
            try:
                async with asyncio.timeout(request_deadline_seconds):
                    output = await original_request(inputs, pbar)
            except TimeoutError:
                timeout = True
                output = client.RequestFuncOutput.init_new(inputs)
                output.error = 'Explicit benchmark request deadline exceeded'
                output.start_time = began
                output.latency = time.perf_counter() - began
            start = output.start_time or began
            def elapsed(name):
                value = observation[name]
                return round(max(0, value - start) * 1000, 3) if value is not None else None
            receipt = {'version': VERSION, 'candidate_model': inputs.model,
                'prompt_sha256': hashlib.sha256(json.dumps(inputs.prompt, sort_keys=True).encode()).hexdigest(),
                'requested_output_tokens': inputs.output_len,
                'request_deadline_seconds': request_deadline_seconds,
                'success': output.success, 'deadline_exceeded': timeout,
                'elapsed_ms': round(output.latency * 1000, 3),
                'first_generated_delta_ms': elapsed('first_delta_at'),
                'first_reasoning_delta_ms': elapsed('first_reasoning_at'),
                'first_content_delta_ms': elapsed('first_content_at'),
                'last_generated_delta_ms': elapsed('last_delta_at'),
                'content_characters': observation['content_characters'],
                'content_contains_think_tag': '<think>' in observation['content_prefix'].casefold(),
                'returned_models': sorted(observation['returned_models']),
                'finish_reason': observation['finish_reason'], 'server_usage': observation['usage'],
                'usage_missing': 'completion_tokens' not in observation['usage'],
                'basis': 'Observed server usage, not requested output length; first content is not a semantic usefulness score.'}
            sink(receipt)
            if answer_sink is not None:
                answer_sink({'version': VERSION, 'prompt_sha256': receipt['prompt_sha256'],
                    'candidate_model': inputs.model, 'success': output.success,
                    'finish_reason': observation['finish_reason'],
                    'content': observation['answer_content'],
                    'content_truncated': observation['content_characters'] > len(observation['answer_content']),
                    'content_contains_think_tag': receipt['content_contains_think_tag'],
                    'scope': 'Private final-channel receipt; never publish unreviewed content.'})
            return output
        finally:
            _active.reset(token)

    client.json = ObservedJSON()
    client.ASYNC_REQUEST_FUNCS['vllm-chat'] = request


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument('--timing-output', required=True, type=Path)
    parser.add_argument('--request-deadline', type=float, default=300)
    parser.add_argument('--answers-output', type=Path, help='Optional private final-channel receipts')
    own, upstream = parser.parse_known_args()
    if upstream[:1] == ['--']:
        upstream = upstream[1:]
    from sglang.benchmark import serving
    if hashlib.sha256(Path(serving.__file__).read_bytes()).hexdigest() != CLIENT_SHA256:
        raise ValueError('The serving client differs from the frozen upstream implementation')
    if '--backend' not in upstream or upstream[upstream.index('--backend') + 1] != 'vllm-chat':
        raise ValueError('This observer qualifies only the vllm-chat backend')
    if '--disable-stream' in upstream:
        raise ValueError('First-content timing requires streaming')
    if '--output-file' not in upstream:
        raise ValueError('Declare an immutable upstream output file')
    destination = Path(upstream[upstream.index('--output-file') + 1])
    paths = [destination, own.timing_output] + ([own.answers_output] if own.answers_output else [])
    if any(path.exists() for path in paths) or len({path.resolve() for path in paths}) != len(paths):
        raise FileExistsError('Choose new performance receipt paths')
    if not 1 <= own.request_deadline <= 1800:
        raise ValueError('Invalid declared request deadline')
    own.timing_output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    from contextlib import ExitStack
    with ExitStack() as resources:
        stream = resources.enter_context(own.timing_output.open('x'))
        answers = resources.enter_context(own.answers_output.open('x')) if own.answers_output else None
        def write(row):
            stream.write(json.dumps(row, sort_keys=True) + '\n')
            stream.flush()
        def write_answer(row):
            answers.write(json.dumps(row, sort_keys=True) + '\n')
            answers.flush()
        install(serving, write, request_deadline_seconds=own.request_deadline,
                answer_sink=write_answer if answers is not None else None)
        sys.argv = [sys.argv[0], *upstream]
        serving.cli_main()


if __name__ == '__main__':
    main()
