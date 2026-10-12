# Google Home device fulfillment rollout

Status: prepared locally, not deployed (2026-09-25).

## Verified findings

- Google OAuth linking succeeds. API fulfillment currently targets `google_home` in ap-south-1, whose deployed code returns a placeholder site scene.
- Digi Store's gateway answered a read-only AWS MQTT health ping and a configuration request from the Mac. This proves gateway cloud reachability, not reachability of every Zigbee endpoint.
- The emulator's Flutter AWS MQTT WebSocket connection fails before connecting.
- `write_state_db` has a disabled SQS mapping for `dataWrite`; stored state was last updated September 15. Approximately 1,469 queued messages were observed; a small sample contained health replies.
- Google proactive discovery and state stream mappings are disabled.
- A read-only run of the proposed discovery against current AWS metadata returned 57 unique supported endpoints: 50 switches, 5 lights, 2 fans. Earlier metadata snapshots contained fewer endpoints; inventory is read at request time.

## Prepared changes

1. `lambdas/google_home_fulfillment/lambda_function.py` replaces site scenes with actual endpoints from user-owned gateway metadata. Stable encrypted IDs match the existing proactive reporter. Supports OnOff, brightness, fan speeds 1–4, and colour temperature for known endpoint types 1, 2, 3, 13. Unsupported types are omitted rather than advertised with nonfunctional commands.
2. QUERY reads real endpoint state. Missing or stale per-endpoint timestamps are unavailable. Gateway health does not refresh old endpoint state. `GH_STATE_MAX_AGE_SECONDS` defaults to 300; this conservative freshness policy needs validation against actual firmware reporting intervals. A gateway answering a ping is not proof that every endpoint is online.
3. EXECUTE verifies ownership through the inventory, validates commands, publishes the existing gateway protocol, and waits up to two seconds for reported state confirmation. Missing confirmation is a timeout, not fabricated success. No optimistic database writes.
4. `lambdas/device_state_writer/lambda_function.py` is a proposed replacement for existing `write_state_db`. It merges actual endpoint reports, timestamps each endpoint independently, handles pings separately, rejects older/duplicate updates, and uses conditional writes/retries to avoid losing concurrent updates. It does not send physical commands or run automations. Its scope is shared across users of the existing queue.
5. Google `willReportState` remains false until the existing proactive reporter is separately corrected and its stream mappings restored. That reporter currently assumes online=true and must not be enabled unchanged.

## Validation

54 focused tests pass, covering authorization, inventory isolation/pagination, stale and missing state, command validation/publish/confirmation, duplicate and out-of-order queue messages, and conditional-write retries. Read-only live discovery succeeds against the configured account. End-to-end physical command verification remains outstanding.

## Proposed AWS deployment (requires shared pipeline decision)

- Back up deployed code/config and mapping details for `google_home` and `write_state_db`.
- Package fulfillment source as `lambda_function.py`; update only `google_home`, retaining its existing environment, crypto layer and role. Do not change OAuth Lambdas or API routes.
- Package state writer as `lambda_function.py`; update only `write_state_db`, retaining its state-table configuration and execution role.
- Re-enable the existing `dataWrite` SQS mapping after code validation. This processes queued messages across users; timestamp checks prevent older endpoint updates replacing newer ones. No messages are purged.
- Verify endpoint records gain independent timestamps when real device reports arrive. Old records stay unavailable until refreshed; do not bulk-mark them online.
- Request Google device sync for the test user and verify real endpoints appear, then test a user-selected light with physical confirmation.
- Inspect the emulator's separate MQTT connection failure if app control is also required.

Rollback: disable the SQS mapping, restore the saved Lambda ZIPs. Restoring the old writer does not remove additional per-endpoint timestamps/version fields; take a data backup before rollout if reverting stored observations is required. No rollout has been performed yet.
