"""Path-only classifier for legacy delivery-sensitive file categories."""

import re
from pathlib import PurePosixPath


ASCII_CASE = re.I | re.A
KEYS = re.compile(r'\.(pem|key|crt|cert|cer|p12|pfx|jks|keystore|ppk|asc|gpg|pgp)$', ASCII_CASE)
ENV = re.compile(r'(^|/)\.env(\..+)?$', ASCII_CASE)
ENV_SUFFIX = re.compile(r'\.env$', ASCII_CASE)
SECRET_NAME = re.compile(
    r'(^|/)[^/]*(credentials?|secrets?|api[-_.]?key|auth[-_.]?token|passwd|shadow)[^/]*$',
    ASCII_CASE)
DB = re.compile(r'\.(sqlite3?|db|dump|sql\.gz)$', ASCII_CASE)
TF_STATE = re.compile(r'(\.tfstate|\.tfvars)($|\.)', ASCII_CASE)
TEMPLATE = re.compile(r'\.(example|sample)(\.[^/]*)?$', ASCII_CASE)


def sensitive_path_category(value):
    """Classify one literal Git path; consumers use -z, --literal-pathspecs and --."""
    if (not isinstance(value, str) or not value or value.startswith(('/', ':')) or
            '\\' in value or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError('candidate path must be a nonempty relative path')
    if (PurePosixPath(value).as_posix() != value or
            any(part in ('', '.', '..') or part.rstrip(' .').casefold() == '.git'
                for part in value.split('/'))):
        raise ValueError('candidate path is not normalized')
    path = value
    if KEYS.search(path):
        return 'key-certificate'
    if ((ENV.search(path) and not TEMPLATE.search(path)) or ENV_SUFFIX.search(path)):
        return 'environment-secret'
    if re.search(r'(^|/)\.(aws|gcloud)/', path, ASCII_CASE):
        return 'cloud-credentials'
    if SECRET_NAME.search(path) and not TEMPLATE.search(path):
        return 'credential-name'
    if re.search(r'(^|/)id_(rsa|dsa|ecdsa|ed25519)', path, ASCII_CASE):
        return 'ssh-private-key'
    if re.search(r'(^|/)service-account[^/]*\.json$', path, ASCII_CASE):
        return 'service-account'
    if DB.search(path):
        return 'database-dump'
    if TF_STATE.search(path) and not re.search(r'\.example$', path, ASCII_CASE):
        return 'terraform-state'
    if re.search(r'(^|/)\.terraform/', path):
        return 'terraform-cache'
    if re.search(r'\.map$', path, ASCII_CASE):
        return 'source-map'
    if re.search(r'\.log$', path, ASCII_CASE) or re.search(r'(^|/)logs/', path):
        return 'log'
    return None
