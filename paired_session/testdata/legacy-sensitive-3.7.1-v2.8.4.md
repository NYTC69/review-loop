Frozen copy of docs/protocol/execution.md section 3.7.1 as of v2.8.4 (2175063), taken from 13c04f2.
v2.8.10 (W02) replaced that section with scripts/security_preflight.py; the paired-session sensitive-path
classifier keeps parity with these pre-W02 legacy grep rules until it is realigned with W02 (BACKLOG follow-up).
Owner exception approved by Yuan on 2026-09-30: only the test data source moved; assertions are unchanged.

### 3.7.1 — Check for tracked or staged sensitive files

Run each of these via Bash and collect every match into a flagged-files
list:

```bash
# Keys & certificates
git ls-files | grep -iE '\.(pem|key|crt|cert|cer|p12|pfx|jks|keystore|ppk|asc|gpg|pgp)$'

# Environment & config secrets (exclude safe .example/.sample templates)
git ls-files | grep -iE '(^|/)(\.env|\.env\..+)$' | grep -v -iE '\.(example|sample)(\.[^/]*)?$'
git ls-files | grep -iE '\.(env)$'

# Credential / secret basenames (exclude .example/.sample)
git ls-files | grep -iE '(^|/)[^/]*(credentials?|secrets?|api[-_.]?key|auth[-_.]?token|passwd|shadow)[^/]*$' \
  | grep -v -iE '\.(example|sample)(\.[^/]*)?$'

# SSH private keys
git ls-files | grep -iE '(^|/)id_(rsa|dsa|ecdsa|ed25519)'

# Cloud service account credentials
git ls-files | grep -iE '(^|/)service-account[^/]*\.json$'

# Cloud credential directories
git ls-files | grep -iE '(^|/)\.(aws|gcloud)/'

# Database dumps / files
git ls-files | grep -iE '\.(sqlite3?|db|dump|sql\.gz)$'

# Terraform state (exclude .example templates)
git ls-files | grep -iE '(\.tfstate|\.tfvars)($|\.)' | grep -v -iE '\.example$'

# Terraform plugin/module cache directory
git ls-files | grep -E '(^|/)\.terraform/'

# Source maps (all variants)
git ls-files | grep -iE '\.map$'

# Log files
git ls-files | grep -iE '\.log$'
git ls-files | grep -E '(^|/)logs/'
```

If flagged files are found, report each as **CRITICAL** and halt. Tell the
user to untrack each file with `git rm --cached <file>` and add the
appropriate pattern to `.gitignore`. Do not proceed.

### 3.7.2
