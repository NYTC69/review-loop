"""Pure Compass close transform; its output is an unreviewed Q proposal, never acceptance."""
import hashlib
import re
from datetime import date


def close_blob(raw, frozen, c1, day):
    if hashlib.sha256(raw).hexdigest() != frozen['backlog_sha256']:
        raise ValueError('Q refuses changed frozen BACKLOG')
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', c1) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('Q needs an exact C1 object ID and ISO closing date')
    text = raw.decode('utf-8')
    chunks = re.split(r'(?m)^## (P[0-3]|Done)\n', text)
    if chunks[1::2] != ['P0', 'P1', 'P2', 'P3', 'Done']:
        raise ValueError('Q refuses malformed BACKLOG sections')
    preamble, bodies = chunks[0], dict(zip(chunks[1::2], chunks[2::2]))
    parsed = {}
    for section, body in bodies.items():
        blocks = re.split(r'(?m)(?=^- )', body)
        prefix, items = blocks[0], blocks[1:]
        if prefix.strip() not in ('', '(none)') or (items and prefix.strip()):
            raise ValueError('Q refuses orphan text or mixed empty-section sentinel')
        parsed[section] = (prefix, items)
    section = frozen['section']
    if section not in ('P0', 'P1', 'P2', 'P3'):
        raise ValueError('Q requires an open source section')
    prefix, items = parsed[section]
    matches = [i for i, block in enumerate(items) if
               re.split(r'\((?:added|closed) ', block.splitlines()[0][2:], maxsplit=1)[0].strip().rstrip('.')
               == frozen['title']]
    if len(matches) != 1:
        raise ValueError('Q refuses missing or ambiguous frozen title')
    source = items.pop(matches[0])
    lines = source.splitlines(keepends=True)
    lines[0] = f'- ~~{lines[0][2:].rstrip()}~~ (closed {day}, see {c1})\n'
    closed = ''.join(lines)
    bodies[section] = prefix + ''.join(items) if items else '\n(none)\n\n'
    done_prefix, done_items = parsed['Done']
    bodies['Done'] = ('\n' if done_prefix.strip() else done_prefix) + ''.join([*done_items, closed][-5:])
    preamble, count = re.subn(r'(?m)^\*\*Last updated\*\*: \d{4}-\d{2}-\d{2}$',
                              '**Last updated**: ' + day, preamble)
    if count != 1:
        raise ValueError('Q requires exactly one Last updated header')
    result = preamble + ''.join('## ' + name + '\n' + bodies[name] for name in bodies)
    return result.encode('utf-8')
