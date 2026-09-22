"""Scope Hermes's unscoped builtin memory on the outgoing request copy.

Hermes renders MEMORY.md and USER.md additively to external memory. Its native
renderer supplies a header and the exact character count, but no ownership
metadata. Restrict those blocks to an attested owner/system participant. The
compatibility test exercises the actual native renderer, including its counts.
This does not edit files, persisted conversations, or user-authored messages.
"""
from __future__ import annotations

import re


_TITLES = r'(?:MEMORY \(your personal notes\)|USER PROFILE \(who the user is\))'
_MARKER = re.compile(r'(?m)^(?:═+\n)?' + _TITLES + r'(?=\s|$)')
_HEADER = re.compile(r'(?m)^═{46}\n' + _TITLES
    + r' \[\d+% — (?P<size>[\d,]+)/[\d,]+ chars\]\n═{46}\n')
_WITHHELD = 'Builtin owner memory is unavailable for this participant.'


class _UnknownNativeMemory(ValueError):
    pass


def _text(value):
    if not isinstance(value, str) or not _MARKER.search(value):
        return value
    output, cursor = [], 0
    while marker := _MARKER.search(value, cursor):
        header = _HEADER.match(value, marker.start())
        if header is None:
            # A changed/truncated native shape cannot establish a safe end.
            # Withhold the instruction carrier, never guess past private text.
            raise _UnknownNativeMemory
        try:
            size = int(header['size'].replace(',', ''))
        except ValueError:
            raise _UnknownNativeMemory from None
        end = header.end() + size
        if end > len(value) or (end < len(value) and not value[end:].startswith('\n\n')):
            raise _UnknownNativeMemory
        output.extend((value[cursor:marker.start()], _WITHHELD))
        cursor = end
    output.append(value[cursor:])
    return ''.join(output)


def _content(value):
    try:
        if isinstance(value, str):
            return _text(value)
        if isinstance(value, list):
            texts = [block['text'] for block in value
                     if isinstance(block, dict) and isinstance(block.get('text'), str)]
            # Native renders a whole memory block as text. Fragmented headers
            # have no independently verified end within a structured part.
            if _MARKER.search(''.join(texts)) and not any(_MARKER.search(text) for text in texts):
                raise _UnknownNativeMemory
            return [{**block, 'text': _text(block['text'])}
                    if isinstance(block, dict) and isinstance(block.get('text'), str)
                    else block for block in value]
    except _UnknownNativeMemory:
        # Withhold the entire carrier, including later parts of a split block.
        return _WITHHELD
    return value


def scope_builtin_memory(request, scope):
    """Shared normal-request and Relay boundary; unknown authority withholds."""
    if (scope is not None and scope.valid_participant
            and getattr(scope, 'authority_lane', '') in {'owner', 'system'}):
        return request
    result = dict(request)
    for field in ('system', 'instructions'):
        if field in result:
            result[field] = _content(result[field])
    for field in ('messages', 'input'):
        if isinstance(result.get(field), list):
            result[field] = [
                {**row, 'content': _content(row.get('content'))}
                if isinstance(row, dict) and row.get('role') in {'system', 'developer'}
                else row for row in result[field]
            ]
    return result
