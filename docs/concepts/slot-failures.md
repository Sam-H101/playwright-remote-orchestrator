# Slot failures

- When the hub can't open a session on the slot it picked (see [Failover](routing.md#failover)), that adds one to the slot's `failCount`. Any session that reaches the Playwright server resets it to 0, so `failCount` counts consecutive connect failures.
- At `SLOT_FAIL_THRESHOLD` (3) the slot is **failing**: the hub skips it, except for one try every `SLOT_FAIL_COOLDOWN_S` (300 s) after its last run. It heals by itself once a session gets through, or right away with **Reset** in the Grid UI.
- Only the hub counts: a client never names a slot, so a refusal it gets (bad token, no free slot in time, unknown node, a `run-server` 428 for its Playwright version, ...) says nothing about one slot, and a check whose site fails got its browser, so the slot worked.
- Checks of the previous release (an old `main.py` still running against the same database) counted their own connect failures; stop or upgrade them with the hub (see [Migrating](../operations/migrating.md)), and **Reset** any slot they left failing.
- Compose passes the same `SLOT_FAIL_*` values to the hub and the UI, so what the UI shows as failing is what the hub skips. Run outside compose, give both the same values (`SLOT_FAIL_THRESHOLD`, also read as `GRID_FAIL_THRESHOLD`, and `SLOT_FAIL_COOLDOWN_S`).

The Grid UI's inspector shows a slot's failures against `SLOT_FAIL_THRESHOLD` and its next retry; see [Grid UI](../grid-ui/index.md).
