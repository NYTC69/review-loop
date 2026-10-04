"""Load-scaled short timeouts for the paired-session test harness (owner 2026-10-04: short test timeouts may grow with load).

Tests call scaled(seconds): factor = clamp(load1 / ncpu, 1, 6), or the PAIRED_SESSION_TEST_TIMEOUT_SCALE override
(clamped the same way). The harness exports the factor in that variable, so a coordinator it starts scales its own
fixed short waits (CLI --version probes, the candidate-test sandbox preflight, the author-probe settle) by the same factor. The product never reads the load
average: with the variable unset its factor is 1 and every production timeout is unchanged.
"""
import math, os

ENV = 'PAIRED_SESSION_TEST_TIMEOUT_SCALE'

def _clamp(value):   # the cap 6 keeps scaled turns shorter than the fakes' hangs (30 s vs ceil(3*6), 120 s vs 10*6)
    return min(6.0, max(1.0, value)) if math.isfinite(value) else 1.0

def env_factor():
    """The product side: the explicit override only, else 1."""
    try: return _clamp(float(os.environ.get(ENV) or 1))
    except ValueError: return 1.0

def factor():
    """The test side: the override when set, else clamp(load1 / ncpu, 1, 6)."""
    if os.environ.get(ENV): return env_factor()
    return _clamp(os.getloadavg()[0] / (os.cpu_count() or 1))

def scaled(seconds):
    return seconds * factor()

def scaled_arg(seconds):
    """An integer CLI value (--timeout is type=int)."""
    return str(math.ceil(scaled(seconds)))
