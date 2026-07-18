---
date: 2026-07-17
topic: web-control-dashboard
---

# Web Control Dashboard

## Summary

Add a responsive single-page LAN dashboard for configuring the Immich casting service, selecting its Chromecast, controlling service-owned playback, and reviewing the last 10 displayed photos.

---

## Problem Frame

The service is currently operated through a TOML file and process signals. Routine tasks such as changing the target Chromecast, adjusting rotation behavior, pausing automation, or confirming what appeared on the TV require host access and provide little immediate feedback. The operator also has no durable view of recently displayed photos.

The intended operator is on the same trusted local network as the service. Internet-facing administration and multiple simultaneous casting targets would add security and coordination needs that this single-household tool does not currently need.

---

## Actors

- A1. LAN operator: Configures and controls the service from a browser on the trusted local network.
- A2. Casting service: Validates settings, discovers receivers, preserves playback ownership rules, and records confirmed displays.
- A3. Chromecast: Advertises itself on the LAN and displays service-owned or external media.
- A4. Immich: Authenticates image requests and supplies eligible assets and previews.

---

## Key Flows

- F1. Configure and select a receiver
  - **Trigger:** A1 opens the dashboard or changes service settings.
  - **Actors:** A1, A2, A3, A4
  - **Steps:** The dashboard shows current non-secret settings and discovered Chromecasts; A1 chooses a receiver and edits values; A2 validates the full change; valid settings are persisted and affected connections are safely reconfigured; invalid settings remain unapplied with actionable feedback.
  - **Outcome:** The dashboard reflects the active persisted configuration without requiring a process restart.
  - **Covered by:** R2, R3, R4, R5, R6, R7
- F2. Control rotation
  - **Trigger:** A1 selects pause, enable, next, reconnect, or stop.
  - **Actors:** A1, A2, A3
  - **Steps:** A2 evaluates current connection and ownership state; safe actions execute once; unsafe stop or next actions are refused; the dashboard reports the resulting state.
  - **Outcome:** A1 can operate the service without weakening protection of external playback.
  - **Covered by:** R8, R9, R10, R11
- F3. Review recent displays
  - **Trigger:** A2 confirms that a service-selected photo is displayed, or A1 opens the dashboard.
  - **Actors:** A1, A2, A4
  - **Steps:** A2 records the confirmed item and timestamp; the dashboard shows the newest 10 entries with thumbnails; old entries fall out of the visible and persisted history.
  - **Outcome:** A1 can identify the recent TV rotation across process restarts.
  - **Covered by:** R12, R13, R14

---

## Requirements

**Dashboard access and status**
- R1. Serve one responsive dashboard suitable for desktop and mobile browsers on the configured trusted-LAN interface.
- R2. Show current service state, rotation enabled or paused state, Chromecast connection state, current ownership classification, and actionable errors without exposing credentials or relay tokens.
- R3. Discover available Chromecasts and present an unambiguous dropdown containing friendly name and UUID, with a way to refresh discovery results.

**Configuration**
- R4. Allow A1 to edit the Immich URL, API key, selected Chromecast, relay settings, rotation interval, debounce, cooldown, timeout, history, and other existing operator-facing configuration values.
- R5. Never return the stored Immich API key to the browser. Show only that a key is configured; a blank key field preserves it and a non-blank value replaces it.
- R6. Validate the complete proposed configuration before persistence or runtime changes. A failed change must preserve the previously active configuration and explain which values need correction.
- R7. Persist valid settings and apply them immediately, safely restarting only affected runtime components while leaving external playback untouched.

**Controls and safety**
- R8. Allow A1 to pause or enable rotation. Pausing prevents new automatic loads and leaves the current receiver media untouched.
- R9. Allow A1 to request the next photo only while the current receiver media is positively owned by this service; otherwise refuse without issuing a Cast load.
- R10. Allow A1 to reconnect the configured Chromecast by discarding the service's current connection and rediscovering the selected device without stopping receiver media.
- R11. Allow A1 to stop casting only while current media is positively owned by this service. Paused, external, unknown, stale, or ambiguous sessions must not receive a stop command.

**Recent history**
- R12. Record an item only after Cast media status confirms that the expected service-owned photo is displayed.
- R13. Persist at most the 10 most recently confirmed items across process restarts, including asset identity and confirmed-display time.
- R14. Show recent items newest first with a safe thumbnail, display timestamp, and asset ID; history thumbnails must not expose the Immich API key or reusable Cast relay capability.

**Operational behavior**
- R15. Keep the existing background-service behavior when no browser is open, and preserve graceful shutdown, bounded retries, structured redacted logs, and conservative ownership handling.
- R16. Treat the trusted-LAN/no-login choice as an explicit deployment boundary and document that the interface must not be exposed to the public internet.

---

## Acceptance Examples

- AE1. **Covers R3, R7.** Given two discovered Chromecasts, when A1 selects a different UUID and saves valid settings, the service persists the selection, reconnects to that receiver, and does not stop media on either device.
- AE2. **Covers R5.** Given a stored API key, when A1 opens or refreshes the dashboard, no response contains that key; saving a blank key field preserves it.
- AE3. **Covers R6.** Given a working active configuration, when A1 submits an unreachable advertised relay address or malformed UUID, the dashboard reports validation errors and the active service continues unchanged.
- AE4. **Covers R8.** Given a service-owned photo on screen, when A1 pauses rotation, the photo remains displayed and no automatic load occurs after the normal interval.
- AE5. **Covers R9, R11.** Given external or ambiguously owned media, when A1 selects next or stop, the service issues no Cast media command and explains that playback is protected.
- AE6. **Covers R10.** Given a disconnected target, when A1 requests reconnect, discovery restarts without requiring a process restart or stopping receiver media.
- AE7. **Covers R12, R13.** Given a transmitted load that never receives matching media confirmation, no history entry is created; after 11 confirmed displays, only the newest 10 survive restart.
- AE8. **Covers R14.** Given recent history entries, when the dashboard loads thumbnails, neither browser-visible URLs nor responses contain the Immich key or a token usable by the Chromecast relay.

---

## Success Criteria

- A LAN operator can complete routine setup, receiver selection, playback control, and recent-history review without editing files or restarting the process.
- Every web-triggered media action retains the service's existing non-interruption guarantees, with explicit no-command behavior for protected states.
- Configuration and the latest 10 confirmed displays survive restart, while the API key remains absent from browser responses and logs.
- The dashboard works on desktop and mobile and communicates active, loading, paused, protected, disconnected, validation-error, and transient-failure states.
- Automated tests cover configuration rollback, secret redaction, command authorization, runtime reconfiguration, and persistent history behavior.

---

## Scope Boundaries

- Support one active Chromecast at a time; discovery may list many devices but the service does not coordinate them simultaneously.
- Trust the local network and do not add login, accounts, roles, TLS termination, or internet-facing deployment support.
- Do not expose a general remote API for arbitrary Cast commands or Immich asset browsing.
- Do not reveal the saved API key, even on demand.
- Do not force-stop external, paused, unknown, stale, or ambiguously owned playback.
- Do not turn recent history into a searchable library, analytics system, or unbounded archive.

---

## Key Decisions

- Use a single dashboard rather than separate setup and operations screens because this is a focused one-operator service.
- Apply valid settings immediately so routine operation does not require host access or a process restart.
- Permit trusted-LAN access without login, while masking secrets and explicitly prohibiting public exposure.
- Gate next and stop controls on positive service ownership; web control does not override the core non-interruption policy.
- Persist settings and exactly 10 confirmed-display history entries so operational context survives restart without creating a broader database product.

---

## Dependencies / Assumptions

- The browser and service run on a trusted LAN, and any reverse proxy or firewall preserves that boundary.
- Chromecast discovery still depends on multicast visibility from the service host.
- Immich remains reachable by the service; browsers do not communicate with Immich using the API key.
- Existing file configuration must have a defined migration or precedence relationship with web-managed persisted settings.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R7][Technical] Define the safest atomic persistence and runtime reconfiguration boundary for settings that affect the relay listener itself.
- [Affects R13][Technical] Choose a minimal durable store for settings and bounded history, including migration from the current TOML configuration.
- [Affects R14][Technical] Define short-lived browser thumbnail delivery that is separate from Chromecast relay capabilities.
