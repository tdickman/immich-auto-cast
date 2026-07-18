# Video Support Status

Video casting is temporarily disabled. The dashboard keeps a disabled video
option to make the status visible, but the management API and coordinator reject
video source selection. If an older state file contains a video source, startup
falls back to the image timeline.

The image sources remain image-only. Timeline, album, person, AI search, event,
and date/location selections continue to request `IMAGE` assets from Immich.

## Issues Found

### Duration Units

Immich's current `AssetResponseDto` returns numeric video duration in
milliseconds. Early support interpreted those values as seconds, causing short
videos to exceed the configured duration limit. Numeric values now divide by
1,000; older string durations such as `00:00:12.5` are also understood.

This parsing fix is retained so future work starts from the correct asset
metadata contract.

### Receiver Codec Compatibility

The Immich playback endpoint serves an encoded-video derivative when one
exists, otherwise it serves the original file. It does not accept a query that
forces H.264 or otherwise negotiates a codec for the requesting receiver.

Testing found valid MP4 playback responses with working byte ranges, but the
video stream used HEVC/H.265. Chromecast models without HEVC support opened the
Default Media Receiver and displayed only its blue Cast icon.

Immich can avoid this for a specific installation by targeting H.264 in Video
Transcoding settings and rerunning the Video Conversion job. That is not a
portable application guarantee because server policy, existing derivatives,
source codecs, and Chromecast capabilities vary.

### HLS Is Not a Drop-In Replacement

Immich has alpha real-time HLS transcoding that can expose H.264 variants, but
it is disabled by default and requires a stateful proxy:

- The master playlist creates an Immich streaming session.
- Child playlists, initialization files, and media segments each require
  authentication.
- Playlist URLs must be rewritten to opaque relay URLs so API credentials never
  reach the Chromecast.
- The relay must proxy and authenticate every child request, select an H.264
  rendition, clean up sessions, and preserve CORS and fMP4 HLS metadata.
- The Cast load must declare HLS and fragmented-MP4 segment formats correctly.

Forwarding only the master URL is insufficient because relative child requests
do not inherit Immich credentials.

### Muting Is Receiver-Wide

The Default Media Receiver does not provide a portable per-item mute option.
The prototype temporarily changed device mute state and restored its previous
value after owned playback. Disconnects, external takeovers, crashes, and user
volume changes make that behavior race-prone and require physical-device tests.

## Existing Prototype

The repository currently retains dormant implementation pieces for:

- video asset type and duration metadata;
- bounded video eligibility;
- opaque video relay capabilities;
- authenticated streaming and single-range forwarding;
- buffered Cast loads;
- media-type ownership metadata and duration-based rotation;
- temporary mute-state restoration;
- per-output duration and mute settings.

Keeping these pieces avoids discarding validated parsing and relay work, but
none should be treated as supported while source selection is disabled.

## Requirements Before Re-Enabling

1. Choose a codec strategy that guarantees H.264 on unsupported receivers.
2. Prefer an HLS-aware authenticated proxy or another bounded transcoding
   design that does not buffer unbounded video data.
3. Detect or configure receiver codec capabilities instead of assuming HEVC.
4. Define behavior for buffering, pause, seeking, completion, load errors, and
   maximum playback time.
5. Make mute handling safe across disconnects, takeovers, crashes, and external
   volume changes, or remove muted mode.
6. Persist media type and duration in history so replay is deterministic.
7. Add physical-device tests for MP4 and HLS requests, ranges, ownership,
   reconnects, completion, stop, and external takeover.
8. Test representative older Chromecasts, Nest displays, Chromecast Ultra, and
   Google TV devices before enabling the option by default.
