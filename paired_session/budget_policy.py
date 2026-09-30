"""Fixed lifecycle budget ceilings and unsigned extension validation."""

from types import MappingProxyType
from typing import NamedTuple


BUDGET_CAPS = MappingProxyType({
    'PLAN': (3, 5), 'EXEC': (6, 8), 'gate': (2, 4), 'FINISH': (4, 6),
    'POLISH-Q': (32, 50), 'specialist': (4, 6), 'analyzer': (4, 6),
    'test-writer': (2, 4), 'simplifier': (2, 4), 'DOCS': (7, 10),
    'final-review': (2, 4), 'SECURITY': (3, 5), 'local-checks': (16, 24),
    'run-calls': (192, 224), 'item-calls': (384, 448), 'epochs': (15, 17),
    'replays': (12, 14), 'rejects': (2, 2), 'launches': (4, 8),
})


class BudgetExtension(NamedTuple):
    run_limit: int
    item_calls_delta: int
    item_epochs_delta: int


def validate_extension(stage, current_limit, by, item_commands, item_calls, item_epochs):
    if stage not in BUDGET_CAPS:
        raise ValueError('unknown budget stage')
    if stage in ('item-calls', 'rejects'):
        raise ValueError('budget stage extends only through its parent or is fixed')
    values = (current_limit, by, item_commands, item_calls, item_epochs)
    if any(type(value) is not int for value in values):
        raise ValueError('budget extension values must be integers')
    if min(current_limit, by, item_commands, item_calls, item_epochs) < 0:
        raise ValueError('budget extension values must be nonnegative')
    if current_limit < BUDGET_CAPS[stage][0]:
        raise ValueError('saved budget is below its fixed default')
    increment_cap = 16 if stage == 'run-calls' else 2
    if by < 1 or by > increment_cap:
        raise ValueError('budget extension step must be positive and within its per-command cap')
    if current_limit + by > BUDGET_CAPS[stage][1]:
        raise ValueError('budget extension exceeds the fixed hard ceiling')
    if (stage == 'run-calls' and
            current_limit - BUDGET_CAPS[stage][0] > item_calls):
        raise ValueError('run-call extension exceeds recorded item total')
    if (stage == 'epochs' and current_limit - BUDGET_CAPS[stage][0] > item_epochs):
        raise ValueError('epoch extension exceeds recorded item total')
    added_calls = by if stage == 'run-calls' else 0
    added_epochs = by if stage == 'epochs' else 0
    if item_commands >= 8 or item_calls + added_calls > 64 or item_epochs + added_epochs > 4:
        raise ValueError('per-item budget extension limit reached')
    return BudgetExtension(current_limit + by, added_calls, added_epochs)
