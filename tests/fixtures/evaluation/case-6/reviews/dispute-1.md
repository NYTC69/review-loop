Reachability: the store is read once at start-up by a single-process CLI; no concurrent writer exists in the shipped tool.
Impact: at worst one stale read that the next run repairs; nothing is written.
Fix cost: advisory locking plus a sidecar checksum and its lifecycle — disproportionate to a stale read.
Requested disposition: MINOR / follow-up (document the single-process assumption).
