# cast-immich

`cast-immich` displays Immich timeline, album, person, AI-search, event, or filtered photos on one or more Chromecasts while each receiver is confidently idle. A trusted-LAN dashboard provides independent source selection, live controls, configuration, history, and queues for every output. The service yields to paused, playing, buffering, external, and unknown sessions.

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

Start the service and open the dashboard locally at `http://127.0.0.1:8080/?password=VALUE`, using the value in `web-password`. It remains available in setup mode when `config.toml` is absent or invalid. The service generates `web-password` with mode `0600` on first start if it does not exist; you can create or replace that file with your own password before starting the service. A valid request authorizes that browser with a ten-year HttpOnly cookie, subject to browser retention limits. The password remains in the initial URL so Android home-screen shortcuts, which may use separate browser storage, retain authenticated access and renew the cookie whenever launched.

```console
export CAST_IMMICH_API_KEY='...'
uv run cast-immich --config config.toml
```

### Raspberry Pi service

On Raspberry Pi OS or another Debian-based systemd distribution, clone the repository and run the installer as your normal user:

```console
./scripts/install.sh
```

The installer installs system packages and `uv` as needed, creates the locked production environment in `.venv`, and installs an `immich-auto-cast.service` system service. The service starts at boot, runs as the checkout owner, and restarts after failures. It continues in dashboard setup mode if `config.toml` does not exist yet. Read `web-password` on the host or use the authenticated QR code to authorize a browser.

The dashboard binds to `127.0.0.1:8080` by default. To make it available to devices on a trusted LAN, reinstall with an explicit bind address (do not expose it to an untrusted network):

```console
CAST_IMMICH_WEB_HOST=0.0.0.0 ./scripts/install.sh
```

Service operations:

```console
sudo systemctl status immich-auto-cast
sudo journalctl -u immich-auto-cast -f
sudo systemctl restart immich-auto-cast
sudo immich-auto-cast-update
```

`immich-auto-cast-update` performs a fast-forward-only pull from the branch's configured Git upstream, synchronizes dependencies from `uv.lock`, and restarts the service. Local configuration, installation identity, password, and state are ignored by Git and remain in the checkout. Re-run the installer if a release changes the systemd installation itself. Back up `config.toml`, `installation-id`, `web-password`, and `state.json`; restoring all four preserves dashboard access, configuration, ownership identity, pause/autocast settings, selected sources, recent-image exclusion, and bounded display history.

`network-online.target` only orders startup; it does not guarantee that Wi-Fi, Immich, or a Chromecast stays available. The application retries discovery and every operational Immich failure indefinitely with bounded cooldowns, while systemd restarts it after an unhandled process failure. Authorization and incompatible API responses are shown as attention conditions but continue retrying. On restart, the persistent installation ID lets the service recognize receiver metadata that still proves ownership, renew the same asset with a fresh relay URL, and continue yielding to external or ambiguous playback.

Confirmed relay URLs remain valid in memory during a running-process outage, but relay tokens and normalized photo bytes are deliberately not persisted. If the Pi restarts while Immich is also unavailable, the Chromecast may retain its decoded image but cannot reliably refetch the old URL. The service keeps retrying and renews that asset automatically once Immich returns. The selected source and recent-image exclusion survive restart; the prepared queue is rebuilt rather than persisted.

To use the dashboard from another trusted-LAN device, bind its separate management listener explicitly:

```console
uv run cast-immich --config config.toml --web-host 0.0.0.0 --web-port 8080
```

There is no dashboard login. Do not expose the management port to the public internet, an untrusted VLAN, or a public reverse proxy. Browser mutations use same-origin and CSRF defenses, but any client with direct trusted-LAN access can operate the service.

The first valid configuration atomically creates `installation-id` beside the configuration. Persist this non-secret file across restarts so the service can recognize its own existing Cast session. `state.json` stores pause/autocast settings, selected sources, recent-image exclusion, and at most 10 confirmed display records; keep it beside the configuration and writable by the service.

## Configuration

- `immich.url`: Immich base URL reachable by the service.
- `outputs`: one or more `[[outputs]]` tables. Each has a stable URL-safe `id`, display `name`, Chromecast `uuid`, discovery/load timeouts, and its own rotation settings.
- `relay.bind_host`: local interface to listen on, normally `0.0.0.0`.
- `relay.advertised_host`: LAN address the Chromecast uses. Loopback and unspecified addresses are rejected.
- `outputs.interval`: seconds from confirmed display until the next selection for that output.
- `outputs.autocast_delay`: idle time before automatic casting, 30 seconds by default. The initial startup cast has no delay.
- `outputs.idle_debounce`: short status-stabilization setting retained for configuration compatibility.
- `outputs.load_timeout`: time allowed for media status to confirm a load.
- `outputs.video_max_duration` and `outputs.video_muted`: reserved video settings. Video selection is temporarily disabled; see [`docs/video-support.md`](docs/video-support.md).
- `outputs.show_web_qr`: overlays a small bottom-left QR code linking to and authenticating with the dashboard. Disabled by default.
- `outputs.web_qr_size`: QR module scale from 1 (tiny, the default) through 6 (largest), including fractional values such as 1.25 or 1.5.
- `outputs.web_qr_position`, `outputs.web_qr_inset_x`, and `outputs.web_qr_inset_y`: per-output corner and exact 1280x720 canvas insets.
- `outputs.web_qr_opacity`: QR badge opacity from 50% through 100%, defaulting to 75%.

The dashboard validates and atomically rewrites the complete TOML configuration. Concurrent stale saves are rejected. Legacy `[chromecast]` plus `[rotation]` files load as a single `default` output without being rewritten until a save. New saves always use `[[outputs]]`. A blank API-key field preserves the file key. When `CAST_IMMICH_API_KEY` is set, it remains authoritative and browser replacement is disabled.

Changing the Immich server origin requires entering a replacement API key in the same save, preventing a stored credential from being forwarded to a different host. The installation identity path becomes immutable after initial setup and must remain relative to the configuration directory.

### Management API

The dashboard uses same-origin JSON endpoints under `/api`. Status, config, discovery, albums, and people are shared. Output operations are scoped under `/api/outputs/{output_id}` for source, seek, reconnect, controls, history, and current/upcoming/history thumbnails. Mutation clients must first read the CSRF token from status/config, then send the exact page `Origin`, `Content-Type: application/json`, `X-Cast-Immich-Request: 1`, and `X-CSRF-Token`. Configuration writes include the current revision; controls include a stable request ID for idempotency. Command responses echo the request ID, command, outcome, and resulting sanitized status.

The selected source can be the normal timeline, an album, a detected person, an Immich AI search term, an event collection, or a date/location filter. Event collections include photos from this day in prior years, favorites from the last 90 days, the previous calendar month, the current season in prior years, and photos of a selected person from the last 365 days. Date bounds are inclusive; city, state/region, and country filters use Immich's metadata fields. Active sources remain image-only. Trashed, archived, offline, video, and audio assets are excluded. Images are normalized before relay and include their capture date beneath the location when Immich provides it. Video selection is visibly disabled in the dashboard and rejected by the management API pending a compatible transcoding design.

Eligible random-search results are retained per source and consumed before Immich is queried again. The pool refills at a low-water mark so the 10-photo queue stays populated without running PostgreSQL's random ordering for every rotation. `On this day` cycles through the matching photos already found after every unique photo has been shown; unavailable assets are removed from that cycle.

## Network

Chromecast discovery uses multicast DNS on UDP 5353. The host also needs outbound Cast connectivity to the device (normally TCP 8009), outbound HTTP(S) to Immich, and the Chromecast needs inbound TCP access to the configured relay port (8787 in the example). Put both devices on the same subnet where possible and disable Wi-Fi client isolation. Permit the relay port from the Chromecast network and, only for trusted clients, the management port (8080 by default) through the host firewall. No inbound management rule is needed while it remains bound to `127.0.0.1`.

The receiver fetches each media item itself. `127.0.0.1`, an isolated container address, or a hostname unavailable to the Chromecast will not work. Host deployment is recommended. If containerizing later, use host networking so mDNS discovery and the advertised relay address remain valid.

## Safety Model

The service owns a session only when versioned media metadata contains its persistent installation ID, a load ID, and the exact current content URL. It requires fresh receiver and media observations before loading. This receiver reports displayed still images as paused, so paused media is retained and controllable only when those complete ownership markers match; all externally paused media remains protected from slideshow controls. The built-in Backdrop receiver is considered idle only when it reports no media session or content; unknown apps and other Backdrop combinations are protected. The dashboard's explicit Stop cast action is the exception: after a fresh status check, it disables autocast, stops media, and terminates any active Cast app on that output.

Chromecast does not provide an atomic “load only if still idle” operation. A user can start playback in the small interval between the final status check and `LOAD`; this is an unavoidable best-effort race. The service minimizes it and never sends a compensating stop, because that could stop the user's new media.

Pause cancels timers and unsent image preparation but leaves current receiver media untouched. Turning autocast off sends `STOP` only after fresh positive ownership; it cannot stop external, unknown, or ambiguously owned media. Turning autocast on also enables automatic rotation and casts immediately when the receiver is idle. After later idle transitions, the dashboard counts down the configured delay before casting again.

## Operations

Logs are structured JSON. API keys, Cast relay URLs, and thumbnail credentials are not logged or returned by management JSON. `SIGINT` and `SIGTERM` stop scheduling, close discovery and HTTP resources, and leave displayed media untouched. Discovery and Immich failures use indefinite bounded retry/cooldown behavior. Unexpected coordinator termination exits the process so its service manager can restart the complete graph. Failed configuration activation restores the previous persisted and active revision.

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
uv run pre-commit install
uv run pytest -m 'not hardware'
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

The pre-commit hooks automatically apply Ruff lint fixes and formatting to staged Python
files. If a hook changes a file, review and stage the result before committing again. Run
`uv run pre-commit run --all-files` to check the entire repository locally.

The opt-in hardware discovery smoke test is read-only:

```console
CAST_IMMICH_TEST_CHROMECAST_UUID='...' uv run pytest -m hardware
```

Loading from the relay should be validated manually on the target model before unattended use, including observed idle/Backdrop states and whether that firmware sends byte-range requests. Also validate duplicate-name discovery, next, explicit owned-media stop, reconnect, and relay changes against the target firmware.
