"""The one credential content rule table (V312-S), shared by both scans:

- scripts/security_preflight.py, the SECURITY stage: every file of the delivery manifest;
- paired_session/leak_scan.py, every route: the lines the run's change adds.

Callers report the rule name and the line only, never the matched value. The rules catch the realistic mistake of
committing a real credential (owner 2026-10-10: users and models are assumed benign); they do not chase obfuscated or
deliberately hidden secrets, and they keep false positives in normal code low. The six rules SECURITY had before the
table keep their exact behavior; the rules added with it skip obvious placeholders and need a random-looking value.
An environment or config lookup is not a quoted literal, so the generic assignment rule never matches it.
"""
import bisect
import re

B64URL = r'[A-Za-z0-9_-]'
# (rule, pattern, filtered). The sources below never match themselves, so the repository carries no matching literal.
RULES = (
    ('private-key-block', re.compile(r'-----BEGIN (?:(?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY|ENCRYPTED PRIVATE KEY)-----'), False),
    ('aws-access-key-id', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', re.A), False),
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b', re.A), False),
    ('slack-token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b', re.A), False),
    ('google-api-key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b', re.A), False),
    ('stripe-live-key', re.compile(r'\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b', re.A), False),
    ('jwt', re.compile(rf'(?<![A-Za-z0-9_-])eyJ{B64URL}{{8,}}\.{B64URL}{{8,}}\.{B64URL}{{8,}}'), True),
    ('anthropic-key', re.compile(rf'(?<![A-Za-z0-9_-])sk-ant-{B64URL}{{20,}}'), True),
    ('openai-project-key', re.compile(rf'(?<![A-Za-z0-9_-])sk-proj-{B64URL}{{20,}}'), True),
    ('sk-key', re.compile(rf'(?<![A-Za-z0-9_-])sk-(?!ant-|proj-){B64URL}{{20,}}'), True),
)
GENERIC_RULE = 'generic-secret-assignment'
# A name containing key/secret/token/password/passwd (api_key included) assigned, or given as a key/value, a quoted literal
# of 20+ characters without spaces; one line only. Applies only to a line no specific rule matched.
GENERIC = re.compile(r'''(?i)(?<![A-Za-z0-9_])["']?[A-Za-z0-9_.-]{0,40}(?:key|secret|token|passwd|password)[A-Za-z0-9_.-]{0,40}'''
                     r'''["']?[ \t]*(?::=|=>|[:=])[ \t]*(["'`])([^"'`\s]{20,})\1''')
PLACEHOLDER_WORDS = ('xxx', 'example', 'dummy', 'changeme', 'change_me', 'change-me', 'placeholder', 'your_', 'your-',
                     'test', 'fake', 'sample', 'redacted')
PREFIX = re.compile(r'^(?:sk-(?:ant-|proj-)?|eyJ)')


def placeholder(value: str) -> bool:
    """xxx, <...>, ${...}, changeme, example, dummy, test, your_..., a single repeated character and the like."""
    lowered = value.lower()
    return (any(word in lowered for word in PLACEHOLDER_WORDS) or '<' in value or '${' in value or '{{' in value
            or len(set(PREFIX.sub('', value))) <= 2)


def random_like(value: str) -> bool:
    """Letters and digits, with one run of 16+ characters between separators (an identifier such as
    user_profile_v2_settings_2024 or a dotted i18n key has only short words)."""
    return (bool(re.search(r'[A-Za-z]', value)) and bool(re.search(r'\d', value))
            and max(len(run) for run in re.split(r'[_.:/-]', value)) >= 16)


def scan_text(text: str) -> list:
    """(rule, line) for every credential in a text, one per rule and line, in line order; never the value."""
    breaks = [match.start() for match in re.finditer('\n', text)]
    line_of = lambda offset: bisect.bisect_left(breaks, offset) + 1
    found = set()
    for rule, pattern, filtered in RULES:
        for hit in pattern.finditer(text):
            if not filtered or (not placeholder(hit.group(0)) and random_like(hit.group(0))):
                found.add((rule, line_of(hit.start())))
    specific = {line for _, line in found}
    for hit in GENERIC.finditer(text):
        line = line_of(hit.start())
        if line not in specific and not placeholder(hit.group(2)) and random_like(hit.group(2)):
            found.add((GENERIC_RULE, line))
    order = {rule: index for index, (rule, _, _) in enumerate((*RULES, (GENERIC_RULE, None, None)))}
    return sorted(found, key=lambda item: (item[1], order[item[0]]))
