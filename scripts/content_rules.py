"""The one credential content rule table (V312-S). Every rule has a scope, and every scan names the scope it runs in:

- WHOLE_DELIVERY: every file of a delivery, whether the run touched it or not. scripts/security_preflight.py scan()
  (the SECURITY stage) runs in this scope and applies only the rules of this scope: the six it had before the table.
  A repository that already holds a JWT-shaped sample or a key-named literal in a file the run never touched is not
  a finding there.
- ADDED_TEXT: text a run adds or publishes. paired_session/leak_scan.py (the lines the run's change adds, every route)
  and security_preflight.scan_file (the review-pr post body) run in this scope and apply every rule.

Scope and placeholder filtering are separate columns: `scope` says where a rule applies, `filtered` says whether a
match must look random and not be a placeholder. Callers report the rule name and the line only, never the matched
value. The rules catch the realistic mistake of committing a real credential (owner 2026-10-10: users and models are
assumed benign); they do not chase obfuscated or deliberately hidden secrets, and they keep false positives in normal
code low. An environment or config lookup is not a quoted literal, so the generic assignment rule never matches it.

V313 narrowed the table on the hits of 704 real commits: a private-key marker needs key material after it, a JWT needs
a signature-length third segment and no certificate chain in its header, a publishable key or a public name is not a
secret, and `|` and `#` separate the words of a composite id.
"""
import base64
import bisect
import json
import re

WHOLE_DELIVERY = 'whole-delivery'
ADDED_TEXT = 'added-text'
B64URL = r'[A-Za-z0-9_-]'
# (rule, pattern, scope, filtered). The sources below never match themselves, so the repository carries no matching literal.
# private-key-block: the BEGIN marker and then key material, i.e. 40+ base64 characters after optional line breaks (real
# ones, or the \n / \r\n escapes of a string literal) and spaces. A regex or a string that only names the markers and
# a template around a value computed at run time are not keys.
RULES = (
    ('private-key-block', re.compile(r'-----BEGIN (?:(?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY|ENCRYPTED PRIVATE KEY)-----'
                                     r'(?:\\[rn]|[\r\n \t])*[A-Za-z0-9+/]{40,}'), WHOLE_DELIVERY, False),
    ('aws-access-key-id', re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b', re.A), WHOLE_DELIVERY, False),
    ('github-token', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b', re.A), WHOLE_DELIVERY, False),
    ('slack-token', re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{20,}\b', re.A), WHOLE_DELIVERY, False),
    ('google-api-key', re.compile(r'\bAIza[0-9A-Za-z_-]{35}\b', re.A), WHOLE_DELIVERY, False),
    ('stripe-live-key', re.compile(r'\b(?:sk|rk)_live_[A-Za-z0-9]{20,}\b', re.A), WHOLE_DELIVERY, False),
    # the third segment is the signature: 43 characters for HS256, the shortest standard one
    ('jwt', re.compile(rf'(?<![A-Za-z0-9_-])eyJ{B64URL}{{8,}}\.{B64URL}{{8,}}\.{B64URL}{{43,}}'), ADDED_TEXT, True),
    ('anthropic-key', re.compile(rf'(?<![A-Za-z0-9_-])sk-ant-{B64URL}{{20,}}'), ADDED_TEXT, True),
    ('openai-project-key', re.compile(rf'(?<![A-Za-z0-9_-])sk-proj-{B64URL}{{20,}}'), ADDED_TEXT, True),
    ('sk-key', re.compile(rf'(?<![A-Za-z0-9_-])sk-(?!ant-|proj-){B64URL}{{20,}}'), ADDED_TEXT, True),
)
# A name containing key/secret/token/password/passwd (api_key included) assigned, or given as a key/value, a quoted literal
# of 20+ characters without spaces; one line only. ADDED_TEXT scope, filtered, and only on a line no rule above matched.
# Not a secret: a publishable key (made to ship in client code), a name that says public, and a JWT-shaped value (the
# jwt rule alone judges those, so a token it lets pass is not caught again under a token name).
GENERIC_RULE, GENERIC_SCOPE = 'generic-secret-assignment', ADDED_TEXT
GENERIC = re.compile(r'''(?i)(?<![A-Za-z0-9_])["']?(?P<name>[A-Za-z0-9_.-]{0,40}(?:key|secret|token|passwd|password)[A-Za-z0-9_.-]{0,40})'''
                     r'''["']?[ \t]*(?::=|=>|[:=])[ \t]*(?P<quote>["'`])(?P<value>[^"'`\s]{20,})(?P=quote)''')
PUBLISHABLE = ('sb_publishable_', 'pk_live_', 'pk_test_')
PUBLIC_NAME = re.compile(r'(?i)public|publishable')
JWT_SHAPED = re.compile(rf'eyJ{B64URL}+\.{B64URL}+\.{B64URL}*')
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
    user_profile_v2_settings_2024, a dotted i18n key or a composite id joined with `|` or `#` has only short words)."""
    return (bool(re.search(r'[A-Za-z]', value)) and bool(re.search(r'\d', value))
            and max(len(run) for run in re.split(r'[_.:/|#-]', value)) >= 16)


def certificate_signed(token: str) -> bool:
    """A JWS whose header has an `x5c` member (a certificate chain) is signed data, such as an App Store signed
    transaction, not a credential. A header that does not decode as JSON has no `x5c`."""
    head = token.split('.', 1)[0]
    try:
        header = json.loads(base64.urlsafe_b64decode(head + '=' * (-len(head) % 4)))
    except ValueError:
        return False
    return isinstance(header, dict) and 'x5c' in header


def credential(rule: str, value: str) -> bool:
    """A filtered rule's match is a credential when it looks random and is neither a placeholder nor signed data."""
    return not placeholder(value) and random_like(value) and not (rule == 'jwt' and certificate_signed(value))


def in_scope(rule_scope: str, scan_scope: str) -> bool:
    """A WHOLE_DELIVERY rule applies in both scans; an ADDED_TEXT rule only in an ADDED_TEXT scan."""
    if scan_scope not in (WHOLE_DELIVERY, ADDED_TEXT):
        raise ValueError(f'unknown scan scope {scan_scope!r}')
    return rule_scope == WHOLE_DELIVERY or scan_scope == ADDED_TEXT


def hits(text: str, scope: str):
    """Every (rule, line) match of the table rules that apply in `scope`, in table order and then text order, repeats
    included (one per match); never the value. The generic assignment rule is not part of this: see scan_text."""
    breaks = [match.start() for match in re.finditer('\n', text)]
    for rule, pattern, rule_scope, filtered in RULES:
        if in_scope(rule_scope, scope):
            for hit in pattern.finditer(text):
                if not filtered or credential(rule, hit.group(0)):
                    yield rule, bisect.bisect_left(breaks, hit.start()) + 1


def scan_text(text: str, scope: str) -> list:
    """(rule, line) for every credential in a text under the rules that apply in `scope` (WHOLE_DELIVERY or ADDED_TEXT),
    one per rule and line, in line order; never the value."""
    found = set(hits(text, scope))
    if in_scope(GENERIC_SCOPE, scope):
        breaks = [match.start() for match in re.finditer('\n', text)]
        specific = {line for _, line in found}
        for hit in GENERIC.finditer(text):
            line, value = bisect.bisect_left(breaks, hit.start()) + 1, hit['value']
            not_secret = value.startswith(PUBLISHABLE) or PUBLIC_NAME.search(hit['name']) or JWT_SHAPED.fullmatch(value)
            if line not in specific and not not_secret and credential(GENERIC_RULE, value):
                found.add((GENERIC_RULE, line))
    order = {rule: index for index, rule in enumerate((*(row[0] for row in RULES), GENERIC_RULE))}
    return sorted(found, key=lambda item: (item[1], order[item[0]]))
