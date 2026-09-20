"""Keep plain chat instructions in one leading message on the outgoing copy.

Some local templates accept only one system message. Do not change roles,
move conversation evidence, or flatten structured/provider-specific content.
"""
from __future__ import annotations


def compact_instructions(request, *, api_mode=''):
    if api_mode not in ('', 'chat_completions') or 'system' in request or 'input' in request:
        return request
    messages = request.get('messages')
    if not isinstance(messages, list) or not messages:
        return request
    first = messages[0]
    if not isinstance(first, dict) or first.get('role') not in ('system', 'developer'):
        return request
    role = first['role']
    count = 0
    for row in messages:
        if (not isinstance(row, dict) or set(row) != {'role', 'content'}
                or row['role'] != role or not isinstance(row['content'], str)):
            break
        count += 1
    if count < 2:
        return request
    return {**request, 'messages': [
        {'role': role, 'content': '\n\n'.join(row['content'] for row in messages[:count])},
        *messages[count:],
    ]}
