# cast-immich

`cast-immich` displays random Immich timeline photos on one Chromecast while the receiver is confidently idle. A trusted-LAN dashboard provides receiver selection, live controls, configuration, and the latest 10 confirmed photos. The service yields to paused, playing, buffering, external, and unknown sessions.

## Requirements

- CPython 3.13
- [`uv`](https://docs.astral.sh/uv/)
- Immich 3.x API key with `asset.read` and `asset.view`
- Chromecast and service host on a network where mDNS and Cast traffic work
- A TCP relay address reachable by the Chromecast

## Install

```console
uv sync --locked
cp config.example.toml config.toml
chmod 600 config.toml
```

Start the service and open the dashboard locally at `http://127.0.0.1:8080`. It remains available in setup mode when `config.toml` is absent or invalid.

```console
export CAST_IMMICH_API_KEY='...'
uv run cast-immich --config config.toml
```

To use the dashboard from another trusted-LAN device, bind its separate management listener explicitly:

```console
uv run cast-immich --config config.toml --web-host 0.0.0.0 --web-port 8080
```

There is no dashboard login. Do not expose the management port to the public internet, an untrusted VLAN, or a public reverse proxy. Browser mutations use same-origin and CSRF defenses, but any client with direct trusted-LAN access can operate the service.

The first valid configuration atomically creates `installation-id` beside the configuration. Persist this non-secret file across restarts so the service can recognize its own existing Cast session. `state.json` stores the pause setting and at most 10 confirmed display records; keep it beside the configuration and writable by the service.

## Configuration

- `immich.url`: Immich base URL reachable by the service.
- `chromecast.uuid`: stable device UUID, not its friendly name. The hardware smoke test or PyChromecast discovery tools can identify it.
- `relay.bind_host`: local interface to listen on, normally `0.0.0.0`.
- `relay.advertised_host`: LAN address the Chromecast uses. Loopback and unspecified addresses are rejected.
- `rotation.interval`: seconds from confirmed display until the next selection.
- `rotation.idle_debounce`: stable-idle period before the first load.
- `chromecast.load_timeout`: time allowed for media status to confirm a load.

The dashboard validates and atomically rewrites the complete TOML configuration. Concurrent stale saves are rejected. A blank API-key field preserves the file key. When `CAST_IMMICH_API_KEY` is set, it remains authoritative and browser replacement is disabled.

Changing the Immich server origin requires entering a replacement API key in the same save, preventing a stored credential from being forwarded to a different host. The installation identity path becomes immutable after initial setup and must remain relative to the configuration directory.

### Management API

The dashboard uses same-origin JSON endpoints under `/api`: `status`, `config`, `discovery`, `controls/{pause|enable|next|stop}`, `reconnect`, `history`, and opaque history thumbnails. Mutation clients must first read the CSRF token from status/config, then send the exact page `Origin`, `Content-Type: application/json`, `X-Cast-Immich-Request: 1`, and `X-CSRF-Token`. Configuration writes include the current revision; controls include a stable request ID for idempotency. Command responses echo the request ID, command, outcome, and resulting sanitized status.

Only normal timeline images are selected. Archived, hidden, locked, trashed, offline, video, audio, and shared-album-only assets are excluded. Timeline-enabled partner assets can be selected because they are part of the API-key user's timeline.

## Network

Chromecast discovery uses multicast DNS on UDP 5353. The host also needs outbound Cast connectivity to the device, and the Chromecast needs inbound TCP access to the configured relay port. Put both devices on the same subnet where possible and disable Wi-Fi client isolation. Permit the relay port and, only for trusted clients, the management port through the host firewall.

The receiver fetches each photo itself. `127.0.0.1`, an isolated container address, or a hostname unavailable to the Chromecast will not work. Host deployment is recommended. If containerizing later, use host networking so mDNS discovery and the advertised relay address remain valid.

## Safety Model

The service owns a session only when versioned media metadata contains its persistent installation ID, a load ID, and the exact current content URL. It requires fresh receiver and media observations before loading. Paused media remains protected even if this service originally loaded it. Unknown apps and unvalidated Backdrop combinations are protected. Dashboard next and stop requests repeat the fresh ownership check at execution time; stale or protected requests issue no media command.

Chromecast does not provide an atomic “load only if still idle” operation. A user can start playback in the small interval between the final status check and `LOAD`; this is an unavoidable best-effort race. The service minimizes it and never sends a compensating stop, because that could stop the user's new media.

Pause cancels timers and unsent image preparation but leaves current receiver media untouched. An explicit dashboard stop sends `STOP` only after fresh positive ownership; it cannot stop paused, external, unknown, or ambiguously owned media. Reconnect never stops receiver media.

## Operations

Logs are structured JSON. API keys, Cast relay URLs, and thumbnail credentials are not logged or returned by management JSON. `SIGINT` and `SIGTERM` stop scheduling, close discovery and HTTP resources, and leave displayed media untouched. Temporary discovery and Immich failures use bounded retry/cooldown behavior. Failed configuration activation restores the previous persisted and active revision.

Troubleshooting:

1. Confirm `relay.advertised_host:port` is reachable from another device on the Chromecast network.
2. Confirm UDP 5353 multicast is not blocked and client isolation is off.
3. Confirm the UUID rather than the friendly name is configured.
4. Confirm the Immich key has `asset.read` and `asset.view`.
5. Confirm the API-key user has eligible timeline images.
6. If the dashboard is remote, confirm `--web-host` is a reachable trusted-LAN interface and TCP port 8080 is allowed only from that LAN.

## Development

```console
uv sync --all-groups
uv run pytest -m 'not hardware'
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The opt-in hardware discovery smoke test is read-only:

```console
CAST_IMMICH_TEST_CHROMECAST_UUID='...' uv run pytest -m hardware
```

Loading from the relay should be validated manually on the target model before unattended use, including observed idle/Backdrop states and whether that firmware sends byte-range requests. Also validate duplicate-name discovery, next, explicit owned-media stop, reconnect, and relay changes against the target firmware.
