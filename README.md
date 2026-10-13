# hatch-remote

Web remote for a Bluetooth-only **Hatch Rest (1st gen)**, driven from a Mac over BLE.

The Mac acts as the bridge: it holds the BLE connection to the Hatch and serves a
small password-protected web UI, so the light/sound can be controlled from a phone
or from anywhere via a Cloudflare tunnel.

## Why not Docker

Docker Desktop on macOS runs a Linux VM with no access to the host Bluetooth radio,
so the BLE side has to run on the host. A local `.venv` keeps it self-contained.

macOS will prompt once to allow your terminal app to use Bluetooth.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then set HATCH_PASSWORD
.venv/bin/python scan.py  # find the device, note its address
```

Set `HATCH_ADDR` in `.env` (or edit the default in `run.sh`) to the address from
`scan.py`. Pinning the address matters if you own more than one Hatch — only the
1st-gen BLE unit speaks this protocol, but pinning avoids ambiguity.

## Run

```sh
./run.sh     # http://localhost:8765  (HOST=0.0.0.0 to expose on the LAN)
```

## CLI

```sh
.venv/bin/python hatch.py status
.venv/bin/python hatch.py on | off
.venv/bin/python hatch.py color 255 0 0
.venv/bin/python hatch.py brightness 128   # 0-255
.venv/bin/python hatch.py volume 89        # 0-255
.venv/bin/python hatch.py sound ocean
```

## Files

| file | what it does |
| --- | --- |
| `hatch.py` | The BLE layer. A `Hatch` class that finds the device, holds one open connection, writes commands and parses state back, plus a CLI wrapper around it. Everything else depends on this; nothing in here knows about HTTP. |
| `server.py` | The web layer. FastAPI app: password login + signed-cookie middleware, a JSON API (`/api/state`, `/api/power/{on}`, `/api/color`, `/api/brightness/{v}`, `/api/volume/{v}`, `/api/sound/{name}`), and the single-page UI served inline as a string at `/`. Holds one module-level `Hatch` behind an `asyncio.Lock`, since concurrent BLE writes fail. |
| `scan.py` | One-shot discovery. Scans 10s and prints any device advertising Hatch's manufacturer ID, so you can get the address for `HATCH_ADDR`. Run it once at setup, or when the device moves or gets replaced. |
| `run.sh` | Launcher. Loads `.env`, refuses to start without `HATCH_PASSWORD`, generates `.secret` on first run, then execs uvicorn on loopback with proxy headers trusted (needed so the tunnel's `x-forwarded-proto` marks the cookie Secure). |
| `.env` | Untracked. `HATCH_PASSWORD`, optionally `HATCH_ADDR`. |
| `.secret` | Untracked. Generated HMAC key for session cookies. Delete it to invalidate every existing login. |

## How `hatch.py` works

The Hatch Rest (1st gen) exposes a small custom GATT service. There is no
official API — the protocol below is reverse-engineered, and this file is a
direct reimplementation of what [`pyhatchbabyrest`](https://github.com/kjoconnor/pyhatchbabyrest)
established, structured around one persistent connection instead of reconnecting
per command.

**Finding the device.** The Hatch advertises manufacturer ID `1076`. `connect()`
either looks up a known address (`HATCH_ADDR`) or scans for the first device
advertising that ID. On macOS, bleak addresses are CoreBluetooth UUIDs, not MAC
addresses — they're stable per-Mac but won't match what another machine sees.

**Two characteristics** do all the work:

```
02240002-5efd-47eb-9c1a-de53f7a2b232   TX        (write commands here)
02260002-5efd-47eb-9c1a-de53f7a2b232   FEEDBACK  (read state here)
```

**Commands** are ASCII strings written to TX, with hex-encoded arguments:

| command | meaning |
| --- | --- |
| `SI01` / `SI00` | power on / off |
| `SC<rr><gg><bb><brightness>` | color **and** brightness together |
| `SV<vol>` | volume |
| `SN<track>` | sound track (see the `Sound` enum) |

There is no separate brightness command — brightness is the fourth byte of the
color command. So `set_brightness()` re-sends the *current* color with a new
brightness, which is why the client has to track state rather than fire and
forget. All values are 0-255.

**State** comes back as a single fixed-layout byte string from FEEDBACK. The
parser asserts on three ASCII markers to make sure the layout hasn't shifted
under it, then reads the fields at known offsets:

```
byte  5  = 'C' (0x43)   marker
bytes 6-8               red, green, blue
byte  9                 brightness
byte 10  = 'S' (0x53)   marker
byte 11                 sound track
byte 12                 volume
byte 13  = 'P' (0x50)   marker
byte 14                 flags; power is off when either of the top two bits is set
```

If a firmware revision moves things around, the asserts fail loudly with the raw
hex rather than silently reporting wrong state.

**The 1.5s wait.** FEEDBACK does not update immediately after a write — it
trails by roughly a second. Reading it back too early returns the *previous*
state, which showed up early on as an "off" command reporting `power: true`. So
`_send()` writes, sleeps 1.5s, then refreshes. This is why every command takes a
couple of seconds; it's a device limitation, not a connection problem.

**Connection handling** is deliberately thin: `_ensure()` reconnects if the link
dropped, and `server.py` retries once on any BLE error before returning a 503.
BLE at this range is flaky enough that retry-and-resync beats trying to keep a
perfect connection.

## Deploying on Linux (systemd)

macOS is a fine place to develop this but a bad place to run it — the laptop
sleeps and moves around. On a small always-on box it becomes a real service.
`hatch.py` needs no changes: bleak talks to BlueZ over D-Bus on Linux instead
of CoreBluetooth.

**The address format changes.** macOS hands out per-machine CoreBluetooth UUIDs
(`6B6C…`); Linux gives you the device's real MAC. The macOS value is meaningless
on the new host, so re-run `scan.py` there and put the MAC in `.env`.

```sh
sudo apt install bluez           # plus python3-venv if it's missing
git clone <repo> /opt/hatch-remote && cd /opt/hatch-remote
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scan.py         # note the MAC
cp .env.example .env             # set HATCH_PASSWORD and the new HATCH_ADDR
./run.sh                         # confirm it works by hand first

sudo cp deploy/hatch-remote.service /etc/systemd/system/   # edit User/paths first
sudo systemctl enable --now hatch-remote
journalctl -u hatch-remote -f
```

Generate a fresh `.secret` on the new host rather than copying it across; that
invalidates existing sessions once, which is what you want when the service moves.

For remote access, run `cloudflared` on the *same* host — see
`deploy/cloudflared-ingress.example.yml`.

### Picking a USB Bluetooth adapter

Most small-form-factor desktops have no Bluetooth at all, so check before
buying: `bluetoothctl list`, `lsusb | grep -i blue`, `rfkill list bluetooth`.

Get a **Realtek RTL8761B/RTL8761BU** (BT 5.x). It's driven by the in-tree
`btrtl`/`btusb` modules, and distributions ship the firmware already — check
with `ls /lib/firmware/rtl_bt/ | grep 8761`. If `rtl8761bu_fw.bin` is there,
the dongle is plug-and-play. After plugging in, confirm the chipset with
`lsusb`: `0bda:8771` is the one you want, because vendors quietly swap chipsets
between hardware revisions of the same model number.

Avoid cheap "CSR 4.0" dongles — the market is full of counterfeit CSR8510
clones that enumerate but misbehave.

Two things matter more than the dongle:

- **Placement.** A tower's rear USB ports are the worst case: metal case, under
  a desk, near the floor. A USB extension cable that gets the dongle up and in
  line of sight is worth more than any upgrade. Check your margin with
  `scan.py`, which prints RSSI — around -85 dBm is the edge of workable.
- **2.4 GHz coexistence.** BLE and 2.4 GHz Wi-Fi share the band. If the host
  also uses a USB Wi-Fi adapter, don't put the two dongles in adjacent ports;
  separate them with the extension cable.

If a non-root user can't reach BlueZ, check the D-Bus policy in
`/usr/share/dbus-1/system.d/bluetooth.conf`. Recent Ubuntu allows
`send_destination="org.bluez"` in the default policy, so no extra rule is
needed; older policies restricted it to root and console users, in which case
add the service user to the `bluetooth` group or ship a D-Bus policy drop-in.

On Linux, the Docker route also becomes possible (`--network=host` plus the
system D-Bus socket mounted) — see the first design note below for why it
isn't on macOS.

## Design notes — three things that bit me

Each of these overrode the original plan, and all three were found by testing an
assumption rather than by reading docs.

**Docker can't see Bluetooth.** The first plan was to containerize the whole
thing to avoid installing anything on the Mac. Docker Desktop on macOS runs a
Linux VM with no passthrough to the host Bluetooth radio, so anything touching
the device has to run on the host. A `.venv` plus a gitignored `.env` got the
same isolation goal — nothing global, delete the folder and it's gone.

**The device lies about its state for about a second.** Early on, an `off`
command came back reporting `power: true`. The cause wasn't a failed write — it
was that FEEDBACK hadn't caught up, so the read-back returned the *previous*
state. Two consecutive commands could report each other's results. The fix is a
1.5s settle before reading, which makes commands feel slow but report honestly.
A UI that cheerfully claims success is worse than one that takes two seconds.

**A `sleep()` is not a rate limit.** The first version of the login throttle was
`await asyncio.sleep(0.5)` on a failed password, which reads like it costs an
attacker half a second per guess. It doesn't — attempts are concurrent, so they
all sleep at once. Measured: **50 wrong passwords in parallel completed in
0.77s**, and the correct password still worked immediately afterward, because
there was no lockout either. Replaced with attempts serialized behind a lock,
a per-IP lockout, and a global ceiling for an attacker rotating IPs. The lesson
generalizes: a defense that was never measured is a comment, not a control.

## Notes

- All values are **0-255**, not percent — the UI shows percentages, the API does not.
- Auth is a single shared password + HMAC-signed cookie (30 days). The signing key
  is generated once into `.secret` so logins survive restarts; delete it to log
  everyone out. Sessions can't be revoked individually.
- The server binds to loopback, so a tunnel (or reverse proxy) plus the password
  are the only way in. `--forwarded-allow-ips='*'` trusts client-supplied
  forwarded headers, which is fine behind a tunnel on loopback and wrong if you
  ever run `HOST=0.0.0.0`.
- Neither the server nor the tunnel survives a reboot — both are started by hand.
