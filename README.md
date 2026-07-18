# cast-immich

`cast-immich` displays Immich timeline, album, person, or AI-search photos on one or more Chromecasts while each receiver is confidently idle. A trusted-LAN dashboard provides independent source selection, live controls, configuration, history, and queues for every output. The service yields to paused, playing, buffering, external, and unknown sessions.

## Requirements

- CPython 3.13
- [`uv`](https://docs.astral.sh/uv/)
- Immich 3.x API key with `asset.read`, `asset.view`, `album.read`, and `person.read`
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

The first valid configuration atomically creates `installation-id` beside the configuration. Persist this non-secret file across restarts so the service can recognize its own existing Cast session. `state.json` stores the pause and autocast settings and at most 10 confirmed display records; keep it beside the configuration and writable by the service.

## Configuration

- `immich.url`: Immich base URL reachable by the service.
- `outputs`: one or more `[[outputs]]` tables. Each has a stable URL-safe `id`, display `name`, Chromecast `uuid`, discovery/load timeouts, and its own rotation settings.
- `relay.bind_host`: local interface to listen on, normally `0.0.0.0`.
- `relay.advertised_host`: LAN address the Chromecast uses. Loopback and unspecified addresses are rejected.
- `outputs.interval`: seconds from confirmed display until the next selection for that output.
- `outputs.autocast_delay`: idle time before automatic casting, 30 seconds by default. The initial startup cast has no delay.
- `outputs.idle_debounce`: short status-stabilization setting retained for configuration compatibility.
- `outputs.load_timeout`: time allowed for media status to confirm a load.

The dashboard validates and atomically rewrites the complete TOML configuration. Concurrent stale saves are rejected. Legacy `[chromecast]` plus `[rotation]` files load as a single `default` output without being rewritten until a save. New saves always use `[[outputs]]`. A blank API-key field preserves the file key. When `CAST_IMMICH_API_KEY` is set, it remains authoritative and browser replacement is disabled.

Changing the Immich server origin requires entering a replacement API key in the same save, preventing a stored credential from being forwarded to a different host. The installation identity path becomes immutable after initial setup and must remain relative to the configuration directory.

### Management API

The dashboard uses same-origin JSON endpoints under `/api`. Status, config, discovery, albums, and people are shared. Output operations are scoped under `/api/outputs/{output_id}` for source, seek, reconnect, controls, history, and current/upcoming/history thumbnails. Mutation clients must first read the CSRF token from status/config, then send the exact page `Origin`, `Content-Type: application/json`, `X-Cast-Immich-Request: 1`, and `X-CSRF-Token`. Configuration writes include the current revision; controls include a stable request ID for idempotency. Command responses echo the request ID, command, outcome, and resulting sanitized status.

The selected source can be the normal timeline, an album, a detected person, or an Immich AI search term. Trashed, offline, video, and audio assets are always excluded; timeline mode also excludes archived, hidden, locked, and shared-album-only assets. The relayed image includes its capture date beneath the location when Immich provides that metadata.

## Network

Chromecast discovery uses multicast DNS on UDP 5353. The host also needs outbound Cast connectivity to the device, and the Chromecast needs inbound TCP access to the configured relay port. Put both devices on the same subnet where possible and disable Wi-Fi client isolation. Permit the relay port and, only for trusted clients, the management port through the host firewall.

The receiver fetches each photo itself. `127.0.0.1`, an isolated container address, or a hostname unavailable to the Chromecast will not work. Host deployment is recommended. If containerizing later, use host networking so mDNS discovery and the advertised relay address remain valid.

## Safety Model

The service owns a session only when versioned media metadata contains its persistent installation ID, a load ID, and the exact current content URL. It requires fresh receiver and media observations before loading. This receiver reports displayed still images as paused, so paused media is retained and controllable only when those complete ownership markers match; all externally paused media remains protected. The built-in Backdrop receiver is considered idle only when it reports no media session or content; unknown apps and other Backdrop combinations are protected. Dashboard next and stop requests repeat the fresh ownership check at execution time; stale or protected requests issue no media command.

Chromecast does not provide an atomic “load only if still idle” operation. A user can start playback in the small interval between the final status check and `LOAD`; this is an unavoidable best-effort race. The service minimizes it and never sends a compensating stop, because that could stop the user's new media.

Pause cancels timers and unsent image preparation but leaves current receiver media untouched. Turning autocast off sends `STOP` only after fresh positive ownership; it cannot stop external, unknown, or ambiguously owned media. Turning autocast on casts immediately when the receiver is idle. After later idle transitions, the dashboard counts down the configured delay before casting again.

## Operations

Logs are structured JSON. API keys, Cast relay URLs, and thumbnail credentials are not logged or returned by management JSON. `SIGINT` and `SIGTERM` stop scheduling, close discovery and HTTP resources, and leave displayed media untouched. Temporary discovery and Immich failures use bounded retry/cooldown behavior. Failed configuration activation restores the previous persisted and active revision.

Troubleshooting:

1. Confirm `relay.advertised_host:port` is reachable from another device on the Chromecast network.
2. Confirm UDP 5353 multicast is not blocked and client isolation is off.
3. Confirm the UUID rather than the friendly name is configured.
4. Confirm the Immich key has `asset.read`, `asset.view`, `album.read`, and `person.read`.
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
