adversarial-gate: REQUEST_CHANGES

### Issues
- [CRITICAL] pkg/store.py:5-7 (confidence=0.55) — torn read: load() takes no lock and verifies no checksum, so a concurrent replace can be observed mid-read.
  Recommendation: add an advisory lock and a sidecar checksum.
