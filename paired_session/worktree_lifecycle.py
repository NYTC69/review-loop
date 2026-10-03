"""Worktree lifecycle W (ADR-11, docs/e2e-6): activation preconditions and the pre-W1b stage HOLD."""
try:
    from paired_session import lifecycle_spine
except ModuleNotFoundError:
    import lifecycle_spine

FORMAT = 'worktree'
FINISH_PENDING = 'worktree lifecycle stage FINISH not implemented yet (W1b)'
WAIVERS = (('accept_unverified_claude_author', '--accept-unverified-claude-author'),
           ('accept_probe_skip', '--accept-probe-skip'))


def refuse_waivers(args):
    """D-7: a lifecycle run needs a real probe PASS; the operator waivers stay real-EXEC only."""
    used = [flag for dest, flag in WAIVERS if getattr(args, dest, False)]
    if used:
        raise ValueError('worktree lifecycle refuses ' + ' and '.join(used) + '; run permission-probe until it passes')


def initial(item_uuid, parent):
    return {**lifecycle_spine.initial(item_uuid, parent), 'format': FORMAT}


def is_worktree(state):
    life = state.get('lifecycle')
    return isinstance(life, dict) and life.get('format') == FORMAT


def refuse_saved(state, args):
    """Only W-format state resumes on the real path; fake, legacy and pre-lifecycle states stay refused."""
    if not is_worktree(state):
        if 'on' in (state.get('config', {}).get('lifecycle_mode'), getattr(args, 'lifecycle_mode', None)):
            raise ValueError('saved lifecycle run cannot resume: it is not a worktree-lifecycle run')
        return
    refuse_waivers(args)   # also on reject/accept/note, whose lifecycle_mode comes from the saved config
    if state.get('claude_author_override') or state.get('probe_skip_override'):
        raise ValueError('worktree lifecycle refuses a recorded author waiver or probe-skip acceptance')


def finish_pending(state):
    return is_worktree(state) and state['lifecycle'].get('stage') == 'FINISH'
