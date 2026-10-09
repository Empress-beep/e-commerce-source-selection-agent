# Replay checklist

- Confirm the real endpoint on the wire
- Freeze one known-good request sample
- Freeze the entry-route response and seeded cookies
- Freeze the bootstrap artifact when public replay still needs it
- Freeze helper vectors such as "abc" or a captured timestamp
- Freeze one raw response sample when decode is involved
- Rebuild dynamic state offline
- Prove envelope build order exactly
- Replay page 1 successfully at least twice
- Verify pagination or cursor advance
- Document list-versus-detail permission boundaries
- Document account-bound or page-specific rules
