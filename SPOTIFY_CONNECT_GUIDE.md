# Spotify Connect — Commercial Hardware Integration Guide

**Version:** 1.0
**Date:** 2026-08-27
**Audience:** Engineering leads, firmware engineers, backend engineers
**Reference:** https://developer.spotify.com/documentation/commercial-hardware

---

## Table of Contents

1. [Launch Process Overview](#1-launch-process-overview)
2. [Pre-requisite: Become an Approved Partner](#2-pre-requisite-become-an-approved-partner)
3. [Step 1: Certification Testing (Certomato)](#3-step-1-certification-testing-certomato)
4. [Step 2: Await Spotify Review — Implications](#4-step-2-await-spotify-review--implications)
5. [Step 4: Access Certomato + eSDK](#5-step-4-access-certomato--esdk)
6. [Threading Model](#6-threading-model)
7. [Event Loop](#7-event-loop)
8. [ZeroConf — mDNS Advertisement](#8-zeroconf--mdns-advertisement)
9. [HTTP Server Implementation](#9-http-server-implementation)
10. [Audio Pipeline — Media Delivery API](#10-audio-pipeline--media-delivery-api)
11. [Seek Binary Search](#11-seek-binary-search)
12. [Credential Storage and Auto-Reconnect](#12-credential-storage-and-auto-reconnect)
13. [Clean Shutdown Sequence](#13-clean-shutdown-sequence)
14. [Hardware Preparation Checklist](#14-hardware-preparation-checklist)

---

## 1. Launch Process Overview

Spotify's commercial hardware certification and distribution process has four steps:

| Step | Description |
|---|---|
| **1** | Test and submit a certification using Spotify's self-testing tool **Certomato** |
| **2** | Send Spotify **two test devices** to Stockholm headquarters (partner pays shipping) |
| **3** | Await **certification approval** from Spotify |
| **4** | Execute a **distribution agreement** and obtain written confirmation |

**Key facts:**
- Commercial Hardware tools and the eSDK are available **only to approved partners**
- Every device requires certification before launch
- Each partner needs a signed distribution agreement
- Firmware setup procedures must be **platform-agnostic**

---

## 2. Pre-requisite: Become an Approved Partner

### Spotify's Current Restriction

> Spotify currently accepts new applications **only from organizations (not individuals)**, and **only if your device integrates a digital voice assistant** — Amazon Alexa or Google Assistant.

Digilux qualifies on both counts:
- **Amazon Alexa** — full App-to-App account linking (production)
- **Google Home / Assistant** — full OAuth + fulfillment stack (production)
- Pursuing the **independent route** (no Systems Integrator)

---

### Route: Independent (No Systems Integrator)

#### Step 1 — Verify Hardware Compatibility
Review Spotify's implementation requirements to confirm your hardware is compatible before applying.

#### Step 2 — Submit the Hardware Partner Application
- Form: https://docs.google.com/forms/d/e/1FAIpQLScs6PAlLoLNzmZxyFEdGIeRdLZ3yAx5WZCh7Qc9jdvonk1pKw/viewform
- Must be submitted by an **organization**, not an individual
- Explicitly state:
  - Applying **without a Systems Integrator**
  - Devices support **Amazon Alexa** (App-to-App linking, production skill)
  - Devices support **Google Home / Google Assistant** (OAuth + fulfillment, production)
  - Organization name: **Digilux**

#### Step 3 — Await Spotify's Review and Approval
- Spotify reviews and contacts you if approved (no published SLA — can be weeks to months)
- No way to track status after submitting

#### Step 4 — Sign Agreements
- **eSDK License Agreement** — grants access to the embedded SDK
- **NDA** — confidentiality terms

#### Step 5 — Access Certomato and eSDK
- Download eSDK builds from Certomato (`certomato.spotify.com`)
- Begin implementation and self-testing

---

### Why Independent Over Systems Integrator

| Factor | Assessment |
|---|---|
| Technical capability | Alexa + Google Home built independently on AWS — proven |
| Architecture fit | AWS-native stack, Flutter app — full control is a natural fit |
| Business interest | SI creates long-term dependency and recurring cost |
| eSDK complexity | Certomato provides self-testing — certification failures are recoverable |

---

### Implications of the Review Step

- **It is a business qualification**, not just technical. Spotify assesses organizational credibility, market viability, and genuine voice assistant integration.
- **No SLA** — do not block your product roadmap on this. Keep building in parallel.
- **Rejection is possible and silent** — incomplete or vague applications are rejected without detailed explanation.
- **Approval triggers legal obligations immediately** — have legal team ready to review eSDK agreement and NDA.
- **No approval = no access**, full stop. Certomato and eSDK are completely inaccessible without approval.

**What to do while waiting:**
- Plan your audio pipeline and firmware architecture for Spotify Connect
- Have legal review NDA and eSDK agreement templates in advance
- Ensure your Alexa skill and Google Action are live/production (not "in development")
- Submit the form now — every day waiting is a day lost

---

## 3. Step 1: Certification Testing (Certomato)

### Pre-requisite: Partner Approval
Certomato is gated — you must be an approved Spotify partner before accessing it.

### 1.1 — Get the eSDK Build from Certomato
- Log into Certomato at `certomato.spotify.com`
- Navigate to the **Builds** section
- Download the appropriate eSDK build for your hardware platform
- The eSDK handles backend negotiations, DRM, and audio delivery

### 1.2 — Create a Spotify Application (Client ID)
- Go to the Spotify Developer Dashboard and create an App
- One **Client ID per brand** — reuse across all Digilux products
- This step does **not** require partner approval and can be done today

### 1.3 — Implement the eSDK on Your Device
- Integrate the eSDK following the relevant guide for your device type
- Review the eSDK API Reference (versions 3.183 and above)

### 1.4 — Self-Test During Development
- Use Certomato **throughout development**, not just at the end
- Certomato provides automated validation and fast feedback
- Run tests iteratively until your device passes all checks

### 1.5 — Submit Certification via Certomato
- Once all tests pass, submit your certification through Certomato
- This is a prerequisite before shipping physical devices (Step 2)

### Flow
```
Partner Approval
      ↓
Download eSDK from Certomato
      ↓
Create App / Get Client ID
      ↓
Implement eSDK on Hardware
      ↓
Self-Test with Certomato (iterative)
      ↓
Submit Certification via Certomato
      ↓
→ Proceed to Step 2 (Ship physical devices)
```

---

## 4. Step 2: Await Spotify Review — Implications

See [Section 2](#2-pre-requisite-become-an-approved-partner) for the full review implications. After certification submission:

- Spotify reviews and approves or rejects your certification
- If approved, proceed to Step 3 (sign distribution agreement)
- Each unique device model requires its own certification
- Firmware upgrades must be possible **at least every 6 months** post-launch

---

## 5. Step 4: Access Certomato + eSDK

### What the eSDK Is

The Spotify embedded SDK (eSDK) is a **compiled binary** you link into your device firmware. It:
- Handles all Spotify backend communication (auth, token refresh, DRM)
- Delivers audio as a **compressed stream** (Ogg/Vorbis, MP3, or FLAC)
- Runs **single-threaded** by design
- Does **not** allocate memory beyond a buffer you provide

### Hardware Requirements

| Requirement | Spec |
|---|---|
| RAM | Minimum **1.4 MB** available |
| TLS heap | ~412 KB |
| Persistent storage | **4 KB** (credentials) |
| Binary footprint | 378 KB (minimal) to 901 KB (TLS + Vorbis) |
| Audio output | **320 kbps** without artifacts, clicks, or glitches |
| Network | UDP/TCP sockets, hostname lookup, mDNS/DNS-SD HTTP server |
| Response latency | Commands (play/pause/skip/seek) within **500ms** standalone |

### Core Initialization — `SpInit`

```c
SpConfig config;
memset(&config, 0, sizeof(config));

config.memory_block       = your_buffer;
config.unique_id          = "aabbccddeeff";    // MAC address — NEVER changes across reboots
config.display_name       = "Digilux Speaker";
config.brand_name         = "digilux";         // must match certification
config.model_name         = "dlx-speaker-v1"; // must match certification
config.device_type        = kSpDeviceTypeSpeaker;
config.product_id         = 0xYOUR_PRODUCT_ID; // unique 32-bit int per model
config.client_id          = "YOUR_CLIENT_ID";
config.error_callback     = your_error_handler;

SpInit(&config);
```

**Key rules:**
- `unique_id` = MAC address of primary network interface — must never change across reboots
- One Client ID per brand — reuse across all Digilux products
- `brand_name` + `model_name` must exactly match your certification application

### Device Identification

| Field | Format | Example |
|---|---|---|
| `product_id` | Unique 32-bit unsigned int per model | `0x00003039` |
| `brand_name;model_name` | Registration format | `digilux;dlx-speaker-v1` |
| `device_type` | Speaker / AVR / Audio Dongle | `Speaker` |

---

## 6. Threading Model

### The Golden Rule
> The eSDK is **not thread-safe**. Every SDK API call must happen on the **same thread that called `SpInit()`**. Violation returns `kSpErrorMultiThreadingDetected` on most platforms, or causes undefined behaviour.

### Architecture

```
┌─────────────────────────────────────────────────────┐
│                  MAIN SDK THREAD                    │
│                                                     │
│   while (running) {                                 │
│       check_message_queue();  ← reads from others  │
│       SpPumpEvents();         ← SDK does its work  │
│       sleep_ms(10);                                 │
│   }                                                 │
└─────────────────────────────────────────────────────┘
         ▲                  ▲
         │ message          │ message
┌────────┴──────┐   ┌───────┴────────┐
│  UI/Button    │   │  ZeroConf HTTP │
│  Thread       │   │  Server Thread │
│               │   │                │
│ flag_play = 1 │   │ queue.push(    │
│               │   │  AddUser req)  │
└───────────────┘   └────────────────┘
```

### Pattern: Flags for Simple Buttons

```c
volatile int flag_play = 0;
void on_button_press() { flag_play = 1; }

while (running) {
    if (flag_play) {
        SpPlaybackPlay();  // safe — on SDK thread
        flag_play = 0;
    }
    SpPumpEvents();
    sleep_ms(10);
}
```

### Pattern: Message Queue for ZeroConf

```c
// ZeroConf HTTP thread — DO NOT call SDK here
void handle_add_user(HttpRequest req) {
    Message msg = { MSG_ADD_USER, req.blob, req.clientKey, req.userName };
    queue_push(&sdk_queue, msg);
    wait_for_response(&msg);      // block until SDK thread processes
    http_respond(msg.result);
}

// Main SDK thread processes queue
while (running) {
    Message msg;
    if (queue_pop(&sdk_queue, &msg)) {
        if (msg.type == MSG_ADD_USER) {
            SpConnectionLoginZeroConf(msg.blob, msg.clientKey, msg.userName);
        }
    }
    SpPumpEvents();
}
```

### Callback Rules
- Return from callbacks **as fast as possible** — they run inside `SpPumpEvents()`
- Do **not** do I/O, heavy computation, or blocking calls inside callbacks
- Only APIs explicitly marked callback-safe can be called from within a callback

---

## 7. Event Loop

### What `SpPumpEvents()` Does
Everything in the eSDK happens inside `SpPumpEvents()`:
- Processes network responses from Spotify backend
- Fires all registered callbacks (playback, connection, volume, seek)
- Handles DRM negotiation and token refresh internally

### Pattern A — Integrated into Existing Firmware Loop (Recommended)

```c
void main_firmware_loop() {
    SpInit(&config);
    register_all_callbacks();

    while (1) {
        handle_button_flags();
        SpPumpEvents();
        handle_audio_output();
        sleep_ms(10);
    }
}
```

### Pattern B — Dedicated Spotify Thread

```c
void spotify_thread_fn() {
    SpInit(&config);
    register_all_callbacks();

    while (running) {
        process_inbound_messages();
        SpPumpEvents();
        sleep_ms(10);
    }
}
// All other threads communicate via message queue only
```

### Hardware Button API Mapping

| Button | SDK Call | Callback |
|---|---|---|
| Play | `SpPlaybackPlay()` | `kSpPlaybackNotifyPlay` |
| Pause | `SpPlaybackPause()` | `kSpPlaybackNotifyPause` |
| Next | `SpPlaybackSkipToNext()` | — |
| Previous | `SpPlaybackSkipToPrev()` | — |
| Volume | `SpPlaybackUpdateVolume()` | `SpCallbackPlaybackApplyVolume()` |
| Shuffle | `SpPlaybackEnableShuffle()` | — |
| Repeat | `SpPlaybackEnableRepeat()` | — |

> Pressing Play on your hardware pulls playback to your device if it was playing elsewhere.

---

## 8. ZeroConf — mDNS Advertisement

### What ZeroConf Is
ZeroConf is how the Spotify app **finds your device on the local network** without manual IP entry. It uses mDNS (RFC 6762) and DNS-SD (RFC 6763).

### Network Layer

| Property | Value |
|---|---|
| Protocol | UDP |
| IPv4 multicast | `224.0.0.251` |
| IPv6 multicast | `FF02::FB` |
| Port | `5353` |
| Domain suffix | `.local.` |
| Must remain active | **Always, while device is running** |

### The Four DNS Records

```
PTR  _spotify-connect._tcp.local.
       └──► Digilux Speaker._spotify-connect._tcp.local.

SRV  Digilux Speaker._spotify-connect._tcp.local.
       └──► 0 0 8080 digilux-speaker-aabbcc.local.

TXT  Digilux Speaker._spotify-connect._tcp.local.
       └──► "CPath=/zeroconf"

A    digilux-speaker-aabbcc.local.
       └──► 192.168.1.50
```

| Record | TTL | Purpose |
|---|---|---|
| PTR | 4500s | Service enumeration — how Spotify app discovers your device |
| SRV | 120s | Host + port — where to connect |
| TXT | 4500s | `CPath=/zeroconf` — which URL path to hit |
| A | 120s | Hostname → IP resolution |

### Three Advertisement Moments

#### 1. Probing (Startup — Verify Hostname Is Unique)
```c
void mdns_probe_hostname(const char *hostname) {
    for (int i = 0; i < 3; i++) {
        mdns_send_query(hostname, DNS_TYPE_ANY);
        sleep_ms(250);
        if (mdns_conflict_detected()) {
            append_suffix_to_hostname();  // rename: -2, -3, etc.
            i = 0;
        }
    }
    mdns_announce();
}
```

#### 2. Announcing (After Probing — Claim the Name)
```c
void mdns_announce() {
    mdns_send_response(PTR | SRV | TXT | A);
    sleep_ms(1000);
    mdns_send_response(PTR | SRV | TXT | A);  // second announcement
}
```

#### 3. Responding (Ongoing — Answer Queries)
```c
void mdns_on_query_received(DnsPacket *query) {
    if (query_asks_for("_spotify-connect._tcp.local.", DNS_TYPE_PTR))
        mdns_send_response(PTR | SRV | TXT | A);

    if (query_asks_for("Digilux Speaker._spotify-connect._tcp.local.", DNS_TYPE_SRV))
        mdns_send_response(SRV | A);

    if (query_asks_for("digilux-speaker-aabbcc.local.", DNS_TYPE_A))
        mdns_send_response(A);
}
```
Response must be delayed **20–120ms** (random) to suppress response storms.

### Goodbye Packets (On Shutdown)
```c
void mdns_goodbye() {
    // TTL=0 removes device from Spotify app immediately
    mdns_send_goodbye_record("Digilux Speaker._spotify-connect._tcp.local.", DNS_TYPE_PTR, TTL_0);
    mdns_send_goodbye_record("Digilux Speaker._spotify-connect._tcp.local.", DNS_TYPE_SRV, TTL_0);
    mdns_send_goodbye_record("Digilux Speaker._spotify-connect._tcp.local.", DNS_TYPE_TXT, TTL_0);
    mdns_send_goodbye_record("digilux-speaker-aabbcc.local.",                DNS_TYPE_A,   TTL_0);
}
```
Without Goodbye packets, device stays visible in Spotify app for up to 75 minutes after going offline.

### IP Change Handling
```c
void on_ip_changed(uint32_t new_ip) {
    mdns_send_goodbye_record("digilux-speaker-aabbcc.local.", DNS_TYPE_A, TTL_0);
    update_a_record(new_ip);
    mdns_send_response(A);  // re-announce with new IP
}
```

### Multiroom / Slave Mode
```c
void on_become_multiroom_slave() {
    mdns_send_goodbye();  // remove from Spotify device list
    mdns_stop();          // stop responding to queries entirely
    // Stopping HTTP server alone is not enough
}
```

### Library Options

| Library | Language | Best For |
|---|---|---|
| **Avahi** | C | Linux-based devices (recommended for Digilux) |
| **lwIP mDNS** | C | RTOS / bare-metal |
| **mdnsd** | C | Lightweight embedded |
| **ESP-IDF mDNS** | C | ESP32 |

### Avahi Example (Linux)

```c
#include <avahi-client/publish.h>

void create_spotify_service(AvahiClient *c) {
    group = avahi_entry_group_new(c, entry_group_callback, NULL);

    avahi_entry_group_add_service(
        group,
        AVAHI_IF_UNSPEC, AVAHI_PROTO_UNSPEC, 0,
        "Digilux Speaker",       // PTR label — appears in Spotify app
        "_spotify-connect._tcp",
        NULL, NULL,
        8080,                    // port
        "CPath=/zeroconf",       // TXT record
        NULL
    );
    avahi_entry_group_commit(group);
}
```

### Testing mDNS (from Mac on same LAN)
```bash
dns-sd -B _spotify-connect._tcp              # browse for devices
dns-sd -L "Digilux Speaker" _spotify-connect._tcp local  # resolve specific device
```

---

## 9. HTTP Server Implementation

### Architecture
```
Spotify App (same LAN)
        │  HTTP/HTTPS
        ▼
Embedded HTTP Server (port 8080 or 443)
        │
        ├── GET  /zeroconf?action=getInfo    → getInfo_handler()
        ├── POST /zeroconf?action=addUser    → addUser_handler()
        └── POST /zeroconf?action=resetUsers → resetUsers_handler()
        │
        │ message queue (never call SDK directly from HTTP thread)
        ▼
Main SDK Thread → SpConnectionLoginZeroConf()
```

### Request Parsing

```
GET:  /zeroconf?action=getInfo&version=2.10.0
POST body (URL-encoded): userName=user&blob=ABC&clientKey=DEF&tokenType=accesstoken
```

The `action` parameter may appear in the **query string OR the body** — parse both. All POST body values must be **URL-decoded** before use.

### Getting Dynamic Values — `SpZeroConfGetVars()`

Call on the SDK thread, cache the result. Never call from the HTTP thread.

```c
SpZeroConfVars vars;
SpZeroConfGetVars(&vars);
// vars.device_id, vars.public_key, vars.remote_name,
// vars.token_type, vars.client_id, vars.library_version, etc.
```

### Endpoint 1: GET `?action=getInfo`

```json
{
  "status": 101,
  "statusString": "OK",
  "spotifyError": 0,
  "responseSource": "Digilux",
  "version": "2.10.0",
  "deviceID": "<SpZeroConfGetVars()>",
  "publicKey": "<SpZeroConfGetVars()>",
  "remoteName": "Digilux Speaker",
  "brandDisplayName": "Digilux",
  "modelDisplayName": "DLX-SPEAKER-V1",
  "tokenType": "<SpZeroConfGetVars()>",
  "clientID": "<SpZeroConfGetVars()>",
  "libraryVersion": "<SpZeroConfGetVars()>",
  "scope": 1,
  "productID": 12345,
  "deviceType": "Speaker",
  "supported_drm_media_formats": [{"drm": 0, "formats": ["audio/vorbis"]}],
  "supported_capabilities": 1
}
```

All strings from `SpZeroConfGetVars()` must be **JSON-escaped** before embedding.

### Endpoint 2: POST `?action=addUser` — Login Flow

```
Spotify App → POST addUser {blob, clientKey, userName}
                  │
HTTP Thread → queue(MSG_ADD_USER) → blocks on semaphore
                  │
SDK Thread  → SpConnectionLogout()  [if logged in]
            → delete_stored_credentials()
            → SpConnectionLoginZeroConf(blob, clientKey, userName)
            → SpPumpEvents()...
            → kSpConnectionNotifyLoggedIn fires
            → sem_post() — unblocks HTTP thread
                  │
HTTP Thread → respond { status: 101, OK }
                  │
SDK Thread  → SpCallbackConnectionNewCredentials(credentials_blob) fires
            → save userName + credentials_blob to flash
```

**Critical rules:**
- Logout existing user **before** calling `SpConnectionLoginZeroConf()`
- Delete stored credentials before new login
- Only respond **after** `kSpConnectionNotifyLoggedIn` fires
- **Never store** the `blob` or `clientKey` — they are temporary
- Store only the `credentials_blob` from the callback

### Endpoint 3: POST `?action=resetUsers` — Factory Reset

```c
SpConnectionLogout();
wait_for_logout();
delete_stored_credentials();
// respond: { status: 101, OK }
```

### Error Code Reference

| Situation | `status` | HTTP | `statusString` |
|---|---|---|---|
| Success | 101 | 200 | `OK` |
| Server/request problem | 102 | 400 | `ERROR-BAD-REQUEST` |
| Unknown error | 103 | 500 | `ERROR-UNKNOWN` |
| Not implemented | 104 | 501 | `ERROR-NOT-IMPLEMENTED` |
| Login failed | 202 | 200 | `ERROR-LOGIN-FAILED` |
| No action parameter | 301 | 400 | `ERROR-MISSING-ACTION` |
| Unknown action | 302 | 400 | `ERROR-INVALID-ACTION` |
| Bad parameters | 303 | 400 | `ERROR-INVALID-ARGUMENTS` |
| SDK API error | 402 | 200 | `ERROR-SPOTIFY-ERROR` |

> HTTP 200 is acceptable for all responses — Spotify app reads `status` from the JSON body.

### HTTPS / TLS

HTTPS is **required** for podcasts, lossless audio, and user privacy. The eSDK ships `spotify_embedded_tls.h` — a TLS abstraction layer. Implement using MbedTLS or OpenSSL (both included as examples in the SDK):

```bash
# CMake
find_package(MbedTLS)   # MbedTLS (custom finder in SDK examples)
find_package(OpenSSL)   # OpenSSL (standard CMake)
```

Rule: When HTTPS is enabled, **disable plain HTTP**.

---

## 10. Audio Pipeline — Media Delivery API

### Architecture

```
Spotify Backend
      │ (DRM-protected compressed audio)
      ▼
eSDK binary  ← handles DRM, fetching, prefetch buffering
      │
      │ on_data(stream_id, data, length)  ← your callback
      ▼
Your Audio Buffer / Decoder
      │  Ogg/Vorbis → PCM  (or MP3, FLAC)
      ▼
DAC / Audio Output → Sound
```

### Dual Buffer Tracking

```
[──────────────────────────────────────────]
 ^                           ^
 playback_pos_ms             delivery_pos_ms
 (currently playing)         (buffered ahead)

Buffer contains: current track tail + prefetched next track head
```

### Registering Callbacks

```c
SpStreamCallbacks callbacks = {
    .on_start        = my_stream_start,
    .on_data         = my_stream_data,
    .on_end          = my_stream_end,
    .on_get_position = my_get_position,
    .on_seek_pos     = my_seek,
    .on_flush        = my_flush,
};
SpRegisterStreamCallbacks(&callbacks, NULL);
```

### Callback Implementations

```c
// New track arriving
void my_stream_start(SpStreamId id, SpStreamInfo *info, void *ctx) {
    current_stream_id = id;
    init_decoder(info->format);
    delivery_pos_ms = 0;
    SpNotifyTrackLength(id, info->duration_ms);
}

// Receive compressed audio chunks
void my_stream_data(SpStreamId id, const void *data, size_t len, void *ctx) {
    ring_buffer_write(&audio_buf, data, len);
    delivery_pos_ms += bytes_to_ms(len);
}

// All bytes delivered
void my_stream_end(SpStreamId id, void *ctx) {
    // Decoder drains buffer, then:
    SpNotifyStreamPlaybackFinishedNaturally(id);
}

// Skip or bitrate change — discard buffered data
void my_flush(SpStreamId id, void *ctx) {
    ring_buffer_clear(&audio_buf);
    delivery_pos_ms = playback_pos_ms;
}

// eSDK asks current playback position
int32_t my_get_position(SpStreamId id, void *ctx) {
    return playback_pos_ms;
}
```

### State Notifications You Must Send

| When | Call |
|---|---|
| Audio starts playing | `SpNotifyStreamPlaybackStarted(stream_id)` |
| Each decoded chunk played | `SpNotifyStreamPlaybackContinued(stream_id, playback_pos_ms)` |
| Track finishes naturally | `SpNotifyStreamPlaybackFinishedNaturally(stream_id)` |
| Track length known | `SpNotifyTrackLength(stream_id, duration_ms)` |
| Decoder error | `SpNotifyTrackError(stream_id, error_code)` |

### Skip and Bitrate Change Flows

```
Skip:
  on_end(current) → on_flush(current) → on_start(next_stream_id) → on_data(next...)

Bitrate change:
  on_flush(current_id) → on_start(new_stream_id, same track) → on_data(new...)
```

### Audio Format Selection

```c
config.supported_drm_media_formats = kSpDrmMediaFormatVorbis  // primary Spotify format
                                   | kSpDrmMediaFormatMp3
                                   | kSpDrmMediaFormatFlac;
```

The SDK ships example decoders for all three formats.

### Playback Callbacks

```c
void SpCallbackPlaybackNotify(SpPlaybackNotify notify, void *ctx) {
    if (notify == kSpPlaybackNotifyBecameActive)   audio_output_start();
    if (notify == kSpPlaybackNotifyBecameInactive) audio_output_stop();
}

// Volume changed from Spotify app
void SpCallbackPlaybackApplyVolume(uint16_t volume, uint8_t remote, void *ctx) {
    set_hardware_volume(volume);
}

// Hardware volume button pressed — keep Spotify app in sync
SpPlaybackUpdateVolume(new_volume);
```

---

## 11. Seek Binary Search

### Why Binary Search

Compressed audio has variable frame sizes — you cannot calculate a byte offset from a timestamp directly. Three methods, in order of preference:

| Method | Format | Complexity |
|---|---|---|
| **Seek tables** (in file) | Ogg/Vorbis | Low — O(log n) table lookup, exact |
| **CBR formula** | MP3 (constant bitrate) | Low — one calculation |
| **Binary search probing** | Any format, fallback | High — iterative |

### Method 1: Seek Tables (Ogg/Vorbis — Primary)

Spotify's Ogg/Vorbis files embed proprietary seek tables. The SDK ships `metadata_page.c` to parse them.

```c
SpotifyMetadata meta;
parse_spotify_metadata(stream_data, &meta);
uint64_t granule     = ms_to_granule(position_ms, meta.sample_rate);
int64_t  byte_offset = seek_table_lookup(meta.seek_table, granule);
SpSetDownloadPosition(stream_id, byte_offset);
SpNotifySeekComplete(stream_id, granule_to_ms(granule, meta.sample_rate));
```

### Method 2: CBR Formula (MP3)

```c
int64_t cbr_seek(int32_t position_ms, int32_t bitrate_bps, int64_t header_bytes) {
    double bytes_per_ms = (double)bitrate_bps / 8.0 / 1000.0;
    return (int64_t)(position_ms * bytes_per_ms) + header_bytes;
}
SpSetDownloadPosition(stream_id, cbr_seek(position_ms, 320000, header_size));
SpNotifySeekComplete(stream_id, position_ms);
```

### Method 3: Binary Search (Universal Fallback)

```c
typedef struct {
    SpStreamId stream_id;
    int64_t    file_size;
    int32_t    target_ms;
    int32_t    tolerance_ms;  // 500ms recommended
    int64_t    lo, hi, probe;
    int32_t    probe_decoded_ms;
    int8_t     seeking;
} SeekState;

void begin_seek(SpStreamId id, int32_t target_ms, int64_t file_size) {
    seek_state = (SeekState){
        .stream_id    = id,
        .file_size    = file_size,
        .target_ms    = target_ms,
        .tolerance_ms = 500,
        .lo           = 0,
        .hi           = file_size,
        .probe        = file_size / 2,
        .seeking      = 1,
    };
    audio_buffer_flush();
    SpSetDownloadPosition(id, seek_state.probe);
}

void evaluate_seek_probe() {
    int32_t diff = abs(seek_state.probe_decoded_ms - seek_state.target_ms);

    if (diff <= seek_state.tolerance_ms || seek_state.hi - seek_state.lo < 512) {
        seek_state.seeking = 0;
        SpNotifySeekComplete(seek_state.stream_id, seek_state.probe_decoded_ms);
        return;
    }

    if (seek_state.probe_decoded_ms > seek_state.target_ms)
        seek_state.hi = seek_state.probe;
    else
        seek_state.lo = seek_state.probe;

    seek_state.probe = seek_state.lo + (seek_state.hi - seek_state.lo) / 2;
    audio_buffer_flush();
    SpSetDownloadPosition(seek_state.stream_id, seek_state.probe);
}
```

### Format Decision Tree

```
on_start() fires
      │
      ├── Ogg/Vorbis → parse metadata page → seek table available?
      │       ├── YES → seek_table_lookup()    ← fastest, exact
      │       └── NO  → binary_search_seek()
      │
      ├── MP3 + CBR → cbr_seek()               ← fast, one calculation
      │
      └── FLAC / VBR MP3 / unknown
              └── binary_search_seek()          ← always works
```

### Seek Rules

| Rule | Why |
|---|---|
| Always flush before `SpSetDownloadPosition()` | Stale data corrupts decoded timestamp |
| Only call `SpNotifySeekComplete()` once per seek | Calling twice causes undefined playback |
| Call `SpNotifyStreamPlaybackContinued()` after resuming | eSDK needs position updates to restart |
| Flush on every binary search probe iteration | Each probe starts with a clean buffer |

### Tolerance Guidelines

| Tolerance | Iterations | Feel |
|---|---|---|
| 100ms | 5–8 | Precise but slower |
| **500ms** | **3–5** | **Recommended — Spotify standard** |
| 1000ms | 2–3 | Fast but noticeable jump |

---

## 12. Credential Storage and Auto-Reconnect

### The Two Blobs — Critical Distinction

| | `blob` + `clientKey` | `credentials_blob` |
|---|---|---|
| **Source** | ZeroConf `addUser` POST body | `SpCallbackConnectionNewCredentials()` |
| **Store to flash?** | **NEVER** | **YES — always** |
| **Expires?** | Immediately after use | Rotates periodically |
| **Used in** | `SpConnectionLoginZeroConf()` | `SpConnectionLoginBlob()` |

### Full Credential Lifecycle

```
FIRST LOGIN (ZeroConf pairing):
  addUser POST → SpConnectionLoginZeroConf(userName, blob, clientKey)
              → kSpConnectionNotifyLoggedIn
              → SpCallbackConnectionNewCredentials(credentials_blob) fires
                  └─► SAVE userName + credentials_blob to flash
                  └─► DISCARD blob + clientKey

REBOOT (auto-reconnect):
  load userName + credentials_blob from flash
  SpConnectionLoginBlob(userName, credentials_blob)
  → kSpConnectionNotifyLoggedIn  ← seamless, no user interaction
  → SpCallbackConnectionNewCredentials() may fire (blob rotated)
      └─► OVERWRITE flash with new credentials_blob
```

### `unique_id` — Most Critical Rule

```c
// CORRECT: MAC address — never changes across reboots
uint8_t mac[6];
get_mac_address("eth0", mac);
snprintf(unique_id, sizeof(unique_id), "%02x%02x%02x%02x%02x%02x",
         mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

// WRONG: Random UUID, IP address, timestamp — anything that changes
```

If `unique_id` changes between reboots, all stored credentials are **instantly invalidated**. Symptom: `"Parsing ZeroConf blob failed with code -3"`.

### Persistent Storage Schema

```c
typedef struct {
    char    user_name[256];
    char    credentials_blob[2048];
    char    unique_id[64];    // store for validation on load
    uint8_t valid;            // 0x01 = valid, 0x00 = empty
} SpotifyCredentials;
```

Store at `/etc/digilux/spotify_creds.bin`. Write atomically (temp file + rename):

```c
int save_credentials(const char *user_name, const char *credentials_blob) {
    SpotifyCredentials creds = {0};
    creds.valid = 0x01;
    strncpy(creds.user_name,        user_name,        sizeof(creds.user_name) - 1);
    strncpy(creds.credentials_blob, credentials_blob, sizeof(creds.credentials_blob) - 1);
    strncpy(creds.unique_id,        DEVICE_UNIQUE_ID,  sizeof(creds.unique_id) - 1);

    int fd = open("/etc/digilux/spotify_creds.bin.tmp", O_WRONLY | O_CREAT | O_TRUNC, 0600);
    write(fd, &creds, sizeof(creds));
    fsync(fd);
    close(fd);
    rename("/etc/digilux/spotify_creds.bin.tmp", "/etc/digilux/spotify_creds.bin");
    return 0;
}
```

### `SpCallbackConnectionNewCredentials` — Full Implementation

```c
void SpCallbackConnectionNewCredentials(const char *credentials_blob,
                                        const char *user_name, void *ctx) {
    // Empty string = permanent logout — Spotify revoked session
    if (!credentials_blob || credentials_blob[0] == '\0') {
        delete_credentials();
        return;
    }
    // Always overwrite — blob rotates periodically
    save_credentials(user_name, credentials_blob);
}
```

### Boot State Machine

```c
typedef enum {
    STATE_LOADING_CREDS,
    STATE_LOGGING_IN_BLOB,
    STATE_LOGGED_IN,
    STATE_WAITING_ZEROCONF,
    STATE_LOGGING_OUT,
    STATE_SHUTDOWN,
} SpotifyState;

SpotifyState g_state = STATE_LOADING_CREDS;

void spotify_main_loop() {
    SpInit(&config);
    register_all_callbacks();
    start_http_server();
    start_mdns();

    while (g_state != STATE_SHUTDOWN) {
        process_message_queue();
        SpPumpEvents();

        if (g_state == STATE_LOADING_CREDS) {
            SpotifyCredentials creds;
            if (load_credentials(&creds) == 0) {
                SpConnectionLoginBlob(creds.user_name, creds.credentials_blob);
                g_state = STATE_LOGGING_IN_BLOB;
            } else {
                g_state = STATE_WAITING_ZEROCONF;
            }
        }
        sleep_ms(10);
    }
    SpFree();
}

void SpCallbackConnectionNotify(SpConnectionNotify notify, void *ctx) {
    switch (notify) {
        case kSpConnectionNotifyLoggedIn:
            g_state = STATE_LOGGED_IN;
            break;
        case kSpConnectionNotifyLoggedOut:
            g_state = (g_state == STATE_LOGGING_OUT) ? STATE_SHUTDOWN : STATE_WAITING_ZEROCONF;
            break;
        case kSpConnectionNotifyError:
            if (g_state == STATE_LOGGING_IN_BLOB) {
                delete_credentials();
                g_state = STATE_WAITING_ZEROCONF;
            }
            break;
    }
}
```

### Error Scenarios

| Scenario | Recovery |
|---|---|
| Credentials rejected | Delete → `STATE_WAITING_ZEROCONF` → user re-pairs |
| Network unavailable on boot | eSDK retries internally — no action needed |
| Remote logout (empty blob callback) | Delete credentials → `STATE_WAITING_ZEROCONF` |
| `unique_id` changed | Delete credentials → `STATE_WAITING_ZEROCONF` → re-pair |

### Standby vs Shutdown

```c
// Standby — device still reachable:
void on_enter_standby() {
    // DO NOT call SpConnectionLogout()
    // DO NOT stop SpPumpEvents()
    audio_output_pause();  // only pause audio
}

// Full shutdown — device going offline:
void on_shutdown() {
    SpConnectionLogout();
    // wait for kSpConnectionNotifyLoggedOut
    // then SpFree(), mDNS goodbye, stop threads
}
```

---

## 13. Clean Shutdown Sequence

### Two Paths

```
Device going offline?
      ├── Still reachable on network? → Soft Standby (pause audio only)
      └── Fully unreachable?          → Full Shutdown (7 phases)
```

### Soft Standby
```c
void on_enter_standby() {
    if (SpPlaybackIsPlaying()) SpPlaybackPause();
    audio_output_mute();
    // Keep SpPumpEvents(), mDNS, HTTP running
}
```

### Full Shutdown — 7 Phases

#### Phase 0: Signal Handling (Linux)
```c
volatile sig_atomic_t g_shutdown_requested = 0;
void signal_handler(int sig) { g_shutdown_requested = 1; }

void register_signals() {
    signal(SIGTERM, signal_handler);
    signal(SIGINT,  signal_handler);
    signal(SIGPIPE, SIG_IGN);
}

while (!g_shutdown_requested) {
    process_message_queue();
    SpPumpEvents();
    sleep_ms(10);
}
initiate_shutdown();
```

#### Phase 1: Stop Accepting New Work
```c
http_server_stop_accepting();
message_queue_set_draining();

// Unblock any HTTP threads waiting on semaphores
SdkMessage msg;
while (queue_try_pop(&sdk_queue, &msg)) {
    msg.result = ERR_SHUTTING_DOWN;
    sem_post(&msg.done);
}
```

#### Phase 2: Drain Active Playback
```c
if (SpPlaybackIsPlaying()) {
    SpPlaybackPause();
    int timeout_ms = 1000;
    while (SpPlaybackIsPlaying() && timeout_ms > 0) {
        SpPumpEvents(); sleep_ms(10); timeout_ms -= 10;
    }
}
audio_pipeline_stop();
if (g_active_stream_id != INVALID_STREAM)
    SpNotifyStreamPlaybackFinishedNaturally(g_active_stream_id);
```

#### Phase 3: Spotify Logout
```c
SpConnectionLogout();

// MUST keep calling SpPumpEvents() — callback fires inside it
int timeout_ms = 5000;
while (g_logout_state == LOGOUT_IN_PROGRESS && timeout_ms > 0) {
    SpPumpEvents(); sleep_ms(10); timeout_ms -= 10;
}
// Proceed after timeout anyway — SpFree will handle it (with delay)
```

> Skipping logout causes `SpFree()` to block for **several seconds**. Always logout explicitly first.

#### Phase 4: SpFree
```c
SpFree();
// After this — DO NOT call any Sp* function
```

#### Phase 5: mDNS Goodbye
```c
mdns_send_goodbye();   // PTR + SRV + TXT + A with TTL=0
sleep_ms(100);         // ensure packets sent before socket closes
mdns_stop();
```

Without Goodbye packets, device stays visible in Spotify app for up to **75 minutes**.

#### Phase 6: Thread Teardown (Reverse Dependency Order)
```c
http_server_stop();
thread_join(http_server_thread,  timeout_ms=2000);

audio_decoder_request_stop();
thread_join(audio_decoder_thread, timeout_ms=2000);

input_thread_request_stop();
thread_join(input_thread,         timeout_ms=1000);

message_queue_destroy(&sdk_queue);
```

#### Phase 7: Hardware Release
```c
audio_dac_release();
network_interface_cleanup();
flash_storage_sync();   // ensure credentials are flushed to disk
log_flush();
```

### Shutdown Orchestrator
```c
void initiate_shutdown() {
    phase1_stop_new_work();
    phase2_drain_playback();
    phase3_spotify_logout();
    phase4_spfree();
    phase5_mdns_goodbye();
    phase6_thread_teardown();
    phase7_hardware_release();
}
```

### Shutdown Timeline
```
T+0ms    SIGTERM → g_shutdown_requested = 1
T+10ms   Phase 1: HTTP stopped, queue drained
T+20ms   Phase 2: SpPlaybackPause() called
T+200ms  Phase 2: Pause confirmed, audio pipeline stopped
T+210ms  Phase 3: SpConnectionLogout() called
T+800ms  Phase 3: kSpConnectionNotifyLoggedOut received (typical)
T+810ms  Phase 4: SpFree() returns
T+820ms  Phase 5: mDNS Goodbye sent
T+920ms  Phase 5: mDNS stopped
T+970ms  Phase 6–7: Threads joined, hardware released
T+980ms  Process exits

Total: ~1 second  |  Worst case (logout timeout): ~6 seconds
```

### systemd Service
```ini
[Unit]
Description=Digilux Spotify Connect Service
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/digilux-spotify
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
KillMode=process
KillSignal=SIGTERM
User=digilux
Group=digilux
ProtectSystem=strict
ReadWritePaths=/etc/digilux

[Install]
WantedBy=multi-user.target
```

### Edge Cases

| Scenario | Behaviour |
|---|---|
| Mid-stream shutdown | `SpPlaybackPause()` → pipeline drains → logout proceeds normally |
| addUser arrives during shutdown | Queue rejects it → HTTP returns `ERROR-BAD-REQUEST` |
| Logout times out (network dropped) | Proceed to `SpFree()` after 5s — adds delay but completes |
| Firmware update | Same as full shutdown but **do not delete** stored credentials |
| SIGKILL / power cut | Device stays in Spotify app up to 75 min — use watchdog + short TTLs to mitigate |

### Shutdown Rules

| Rule | Why |
|---|---|
| `SpPlaybackPause()` before logout if playing | Tells Spotify app device paused — not disappeared |
| `SpConnectionLogout()` before `SpFree()` | Skipping causes SpFree to block several seconds |
| Keep calling `SpPumpEvents()` during logout wait | Callback only fires inside SpPumpEvents |
| Send mDNS Goodbye after `SpFree()` | Without it, device visible in Connect menu up to 75 min |
| Stop HTTP before `SpFree()` | New requests after SpFree crash on dead SDK |
| Drain message queue before `SpFree()` | Unblocks HTTP threads waiting on semaphores |
| Set logout timeout (5s) | Prevents indefinite stall on network failure |
| Never call `Sp*` after `SpFree()` | SDK memory freed — undefined behaviour |
| `flash_storage_sync()` before hardware release | Prevents credential loss on power cut during write |

---

## 14. Hardware Preparation Checklist

Use this checklist before partner approval arrives so implementation starts immediately.

### Network
- [ ] Device can join multicast group `224.0.0.251` on Wi-Fi/Ethernet interface
- [ ] UDP port 5353 is available (`ss -ulnp | grep 5353`)
- [ ] Network stack supports UDP multicast (check RTOS/lwIP config)
- [ ] mDNS library chosen: **Avahi** (Linux) or **lwIP mDNS** (RTOS)
- [ ] Device can run an embedded HTTP server (mongoose, libmicrohttpd, etc.)

### Memory
- [ ] **1.4 MB RAM** free after firmware loads
- [ ] **4 KB persistent storage** available (flash sector, EEPROM, or file)
- [ ] Storage supports atomic writes (temp + rename, or transactional NVS)

### Audio
- [ ] Ogg/Vorbis decode capability confirmed (SDK ships example decoder)
- [ ] Audio pipeline supports ring buffer with dual-stream prefetch
- [ ] DAC/audio output can be controlled programmatically (volume, mute, start/stop)
- [ ] 320 kbps output without artifacts confirmed on hardware

### Firmware / Platform
- [ ] MAC address of primary network interface is accessible from firmware
- [ ] Main event loop identified (where `SpPumpEvents()` will live)
- [ ] RTOS supports message queues between threads
- [ ] TLS library available: MbedTLS or OpenSSL

### Spotify Developer Dashboard (Do Today — No Approval Needed)
- [ ] Create a Spotify App at developer.spotify.com
- [ ] Record your **Client ID** (one per Digilux brand)
- [ ] Assign a unique **32-bit Product ID** per device model

### Legal (Prepare in Advance)
- [ ] Legal team briefed on upcoming eSDK License Agreement review
- [ ] Legal team briefed on NDA review
- [ ] Alexa skill confirmed as **live/production** (not development stage)
- [ ] Google Action confirmed as **live/production** (not development stage)

---

*Document version 1.0 — 2026-08-27*
*Reference: https://developer.spotify.com/documentation/commercial-hardware*
*Digilux voice integrations: `/Users/maheshmaney/maney/digilux/app-to-app`*
