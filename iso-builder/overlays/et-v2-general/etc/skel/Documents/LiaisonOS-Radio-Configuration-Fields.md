# LiaisonOS — Radio Configuration: Field Reference

**App:** `et-radio-config` (Radio Configuration / *Configuration Radio*)
**Type:** Local web UI (Flask + PyWebView window, port 5057)
**Purpose:** View, create, edit, duplicate and delete **radio profiles** — the JSON
files that tell emcomm-tools how to talk to your radio (CAT control, PTT, VARA FM,
Bluetooth TNC, audio levels).

This document explains **every field** in the editor, what it does, its valid
values, and where it ends up in the saved JSON.

---

## How radio profiles are stored

- Each radio is one JSON file in `radios.d`.
- **Stock radios:** `/opt/emcomm-tools/conf/radios.d/*.json` (shipped, read-only).
- **Your radios:** saved to `~/.config/emcomm-tools/radios.d/<id>.json`, and a
  **symlink** is created in the system dir so the rest of emcomm-tools sees it.
  (In the radio list, your profiles show as **Custom**; shipped ones as **Stock**.)
- The **active** radio is the symlink `radios.d/active-radio.json`. You cannot
  delete the radio that is currently active — switch radios first.
- **Filename is derived automatically** from Vendor + Model (see *ID & Filename*
  at the end). A Bluetooth radio is saved as `<id>.bt.json`; everything else as
  `<id>.json`.

The editor groups fields into collapsible **sections**. CAT Control, VARA FM,
Bluetooth and Audio each have an **Enable** toggle — when a section is disabled,
that block is simply left out of the saved JSON.

---

## 1. Core Settings *(always visible)*

| Field | Required | JSON key | Notes |
|-------|----------|----------|-------|
| **Vendor** | ✅ | `vendor` | Manufacturer, e.g. `Icom`, `Yaesu`, `Kenwood`, `BG2FX`. Used (with Model) to build the ID/filename. |
| **Model** | ✅ | `model` | Model name, e.g. `IC-705`, `FT-891`, `FX-4CR`. |
| **ID** | auto | `id` | **Read-only preview.** Auto-generated slug of `vendor-model` (lowercased, spaces/symbols → `-`, parentheses dropped). E.g. *Icom + IC-705* → `icom-ic-705`. You never type this; it also becomes the filename. |
| **Bands** | optional | `bands` | Checkboxes **HF / VHF / UHF**. Select every band the radio covers. Saved as a list, e.g. `["HF"]` or `["VHF","UHF"]`. Lets the rest of the system offer the radio only for modes it can run. |

---

## 2. CAT Control (rigctrl) *(toggle — "CAT Control (rigctrl)")*

Controls how **Hamlib `rigctld`** talks to the radio for frequency/mode/PTT over
a serial/USB (CI-V/CAT) link. Enable this only if the radio has computer control.

> **If you leave this section OFF**, the profile is saved with `"cat": false`.
> That tells QtDashboard the radio has no CAT (shows "CAT: N/A" instead of a red
> error) so a no-CAT setup — e.g. an HT + DigiRig Lite using VOX or serial PTT —
> won't get blocked at the CAT precondition check.

| Field | Required | JSON key | Valid values | Notes |
|-------|----------|----------|--------------|-------|
| **Hamlib Rig ID** | ✅ (when enabled) | `rigctrl.id` | A Hamlib model number | The `rigctld -m <ID>` model number. Find it with `rigctl -l` (e.g. `3085` = Icom IC-705). **`1` = Hamlib "Dummy"** — i.e. no real CAT radio (treated as *audio-only*). |
| **Baud Rate** | optional | `rigctrl.baud` | `--`, `4800`, `9600`, `19200`, `38400`, `57600`, `115200` | Serial speed of the CAT link; must match the radio's menu setting. `--` leaves it unset (Hamlib default for that model). |
| **PTT Method** | optional | `rigctrl.ptt` | `RIG`, `RTS`, `DTR`, `NONE` | How `rigctld` keys the transmitter. **RIG** = via CAT command (most modern rigs); **RTS**/**DTR** = assert that serial control line; **NONE** = rigctld does not key PTT (e.g. VOX or an external keyer). Defaults to `RIG`. |
| **Hamlib --set-conf** | optional | `rigctrl.conf` | comma-separated `key=value` list | Advanced per-rig serial/behaviour tweaks passed to `rigctld --set-conf`. Examples: `dcd_type=NONE`, or `data_bits=8,serial_handshake=None,stop_bits=1,rts_state=OFF` (the FX-4CR needs `rts_state=OFF` so RTS doesn't block audio). Leave blank unless a radio needs it. |
| **PTT Only Mode** | optional | `rigctrl.pttOnly` | checkbox → `"true"` | Use the rig connection **only for PTT keying**, not for full frequency/mode control. For radios where CAT is flaky but the line can still key TX. |
| **Prime Rig on Connect** | optional | `rigctrl.primeRig` | checkbox → `true` | Send an initial setup/"prime" to the rig when the session connects (puts it into the expected state). Enable only if your radio needs a kick to be ready. |

### PTT Method — in depth

`rigctrl.ptt` tells `rigctld` **how to key transmit** (it maps directly to
Hamlib's `rigctld -P <TYPE>`). Pick it by how your radio/interface is wired:

- **RIG** — PTT over the **CAT link itself**: rigctld sends the radio's own
  "transmit" command down the same serial/USB CAT connection. Best for modern
  CAT-capable rigs (Icom CI-V, Yaesu/Kenwood CAT — IC-705, FT-891, …). No extra
  wiring, but it needs the CAT link to be working. **Start here for a USB-CAT radio.**
  
- **RTS** (*Request To Send*) — PTT by raising the serial port's **RTS**
  hardware control line. Use when a sound-card interface/cable keys TX from RTS
  (e.g. DigiRig's PTT line). rigctld asserts RTS to transmit; CAT (if present)
  can still run for frequency/mode.
  
- **DTR** (*Data Terminal Ready*) — same as RTS but on the **DTR** line. Whether
  to pick RTS or DTR depends purely on how your cable/interface is wired — if
  RTS doesn't key the rig, try DTR.

  *(RTS and DTR are RS-232 serial control lines — originally modem handshake
  signals, now exposed by USB-serial adapters (CP2102/CH340/FTDI) as output pins
  that software can toggle. A ham interface wires one of them to the radio's PTT,
  so raising the line keys transmit.)*
  
- **NONE** — rigctld **does not key PTT at all**. Transmit is handled some other
  way: **VOX** (the radio transmits when it hears audio), an external keyer, or
  the mode's own PTT (e.g. VARA FM's separate PTT settings). Use for audio-only
  setups, or when CAT is only there for tuning.

**Tips**
- List the PTT types a model supports: `rigctl -m <ID> --dump-caps | grep -i ptt`.
- If **Test CAT shows a frequency but the rig never transmits**, PTT Method is
  almost always the cause — try `RIG`, then `RTS`, then `DTR`, pressing the **PTT**
  button after each until it goes green (it keys the rig directly, no mode needed).
- No computer PTT at all on the radio → **`NONE` + VOX**.
- Related: **PTT Only Mode** (use the link for keying only, not full CAT), and —
  for VARA FM — that mode's own PTT fields in the VARA FM section below.

### Built-in CAT helpers *(LiaisonOS 2.4.5+)*

The CAT Control section now has buttons so you don't need a terminal:

- **Find… (beside Hamlib Rig ID)** — searches Hamlib's model list (`rigctl -l`)
  by vendor/model and fills the **Rig ID** for you (type "IC-705" → picks `3085`).
- **Test CAT** — reads the current frequency from the **live rigctld** that
  LiaisonOS already runs (CAT is engaged the moment the rig's USB is connected).
  **Green + a frequency** = your active radio's CAT is responding. It does *not*
  start its own rigctld and changes nothing — a pure read-back of the engaged link.
- **PTT** — a transmit test, like WSJT-X / JS8Call, **without launching any mode**.
  Click it and LiaisonOS keys the rig through the same live rigctld (using whatever
  **PTT Method** the active profile set), then reads PTT back: **green "ON AIR"** =
  genuinely transmitting; click again to unkey (back to normal); **red** = it didn't
  key (check PTT Method / cabling) or CAT isn't engaged. It **transmits**, so test
  into a dummy load or on a clear frequency. Safety: it **auto-releases after 10 s**
  and if you leave the page while keyed. Changes no config — it only drives rigctld's
  PTT line.

The VARA FM section has one more helper:

- **Detect COM (beside PTT Port)** — finds which Wine COM port actually reaches
  your radio and fills the **PTT Port** field with it. In LiaisonOS that is
  **`COM10`** (linked to `/dev/et-cat`); the button proves it and, if your prefix
  ever differs, reports the correct port instead. You never have to look up a COM
  number by hand. Read-only — it inspects the Wine port links only and writes nothing.

---

## 3. VARA FM *(toggle — "VARA FM")*

Settings for **VARA FM** (the Windows soundcard modem run under Wine). These map
to VARA FM's INI/PTT configuration. Only fill this in if you use VARA FM with this
radio.

| Field | JSON key | Valid values | Notes |
|-------|----------|--------------|-------|
| **PTT Port (Wine COM)** | `varafm.pttPort` | a Wine COM port — **`COM10`** | The virtual COM port VARA FM uses to key PTT (as seen inside Wine). **`COM10` is the LiaisonOS standard — it maps to `/dev/et-cat`, the stable link to your connected rig (or DigiRig). Leave it at `COM10`** unless the **Detect COM** button says otherwise. |
| **PTT Via** | `varafm.pttVia` | `0` VOX · `1` CAT · `2` COM (RTS/DTR) · `3` CM108 | VARA FM's PTT mode. **0** = VOX (audio-triggered); **1** = CAT command; **2** = a serial COM line (then set DTR/RTS below); **3** = CM108 USB-soundcard GPIO keying. Defaults to `2`. *(Note: older builds mislabeled these — `1` is CAT, not RTS.)* |
| **Baud Rate** | `varafm.baud` | e.g. `19200` | Baud for the VARA FM PTT COM port (free text). |
| **DTR** | `varafm.dtr` | `0` / `1` | When *PTT Via = 2 (COM)*, set `1` if PTT is keyed by asserting the **DTR** line. |
| **RTS** | `varafm.rts` | `0` / `1` | When *PTT Via = 2 (COM)*, set `1` if PTT is keyed by asserting the **RTS** line. |

> Only the fields you fill are saved; empty ones are dropped from the JSON.

### VARA FM PTT — in depth

VARA FM is a Windows soundcard modem; LiaisonOS runs it under Wine. During a VARA
FM session **VARA keys the transmitter itself**, so it has its *own* PTT settings
— separate from the CAT Control **PTT Method** above (which is for `rigctld` and
other modes). These fields are those VARA settings.

- **PTT Port (Wine COM)** — the serial port VARA uses to key PTT, named as Wine
  sees it, which maps to the host's real serial device. Needed when *PTT Via* is
  **CAT** or **COM**; ignored for VOX and CM108. **Use `COM10`** — in LiaisonOS
  that Wine port is linked to `/dev/et-cat`, the stable device for whatever rig or
  DigiRig is connected, so it always reaches the radio. You normally never change
  this: new profiles prefill `COM10`, the **Detect COM** button confirms it, and
  LiaisonOS re-links `COM10 → /dev/et-cat` on every VARA FM start. *(Older Icom
  profiles shipped `COM5`, which pointed at a dead port — fixed in 2.4.5.)*
- **PTT Via** (VARA's `PTTMode`):
  - **0 = VOX** — VARA doesn't key a line; the radio's **VOX** transmits when it
    hears VARA's audio. No PTT wiring, but depends on VOX hang-time.
  - **1 = CAT** — VARA keys PTT by sending a **CAT command** out the PTT Port.
    Use when the rig takes PTT over CAT (point the PTT Port at the rig's CAT COM).
  - **2 = COM (RTS/DTR)** — VARA keys PTT by raising a serial **control line**
    (RTS and/or DTR) on the PTT Port; set the **DTR**/**RTS** fields to say which.
    The common sound-card-interface case.
  - **3 = CM108** — VARA keys PTT via a **CM108/CM119** USB-audio chip's GPIO pin
    (many cheap USB dongles and DigiRig-style interfaces). No COM port needed.
  - *Default `2`. (Older builds mislabeled these — `1` is CAT, not RTS.)*
- **DTR** / **RTS** (`0`/`1`) — only used when *PTT Via = 2 (COM)*. Set to `1` the
  line your interface uses to key PTT (DTR, RTS, or both). Same RS-232 control
  lines as in the CAT section — just driven by VARA FM here instead of `rigctld`.
- **Baud Rate** — serial speed for the PTT COM port (matters for the CAT-over-COM
  PTT case).

> **Why two PTT settings?** `rigctld` (CAT Control section) owns the radio's
> frequency/mode and keys PTT for most modes; **VARA FM runs its own modem and
> keys PTT itself** during a VARA session. Configure whichever matches how that
> mode drives your rig.

---

## 4. Bluetooth TNC *(toggle — "Bluetooth TNC")*

For radios that connect over **Bluetooth** as a KISS TNC (e.g. Kenwood TH-D74,
BTECH UV-Pro). Enabling this section makes the profile save as `<id>.bt.json` and
marks the connection type as *Bluetooth*.

| Field | JSON key | Valid values | Notes |
|-------|----------|--------------|-------|
| **Device Name** | `bluetooth.deviceName` | e.g. `TH-D74` | The Bluetooth device name to pair/connect to. |
| **RFCOMM Channel** | `bluetooth.channel` | `1` / `2` | The RFCOMM serial channel the radio exposes for KISS. Most use `1`. |
| **MAC Address** | `bluetooth.mac` | `AA:BB:CC:DD:EE:FF` | The radio's Bluetooth MAC address. |

---

## 5. Audio *(toggle — "Audio")*

| Field | JSON key | Notes |
|-------|----------|-------|
| **ALSA Setup Script** | `audio.script` | Full path to a shell script that sets the ALSA mixer levels for this radio's sound interface (PCM/Mic/AGC, etc.), e.g. `/opt/emcomm-tools/conf/radios.d/audio/my-radio.sh`. Run when the radio is selected so levels are correct for digital modes. |

---

## 6. Configuration Notes *(always visible)*

| Field | JSON key | Notes |
|-------|----------|-------|
| **Notes** | `notes` | Free text, **one note per line**. Saved as a list. These are **operator-facing setup steps** — the manual knob/button sequence to prep the radio (e.g. "Toggle [SSB] until USB and DIG appear", "Set mic gain to 50", "alsamixer PCM = 21"). Shown to whoever sets up this radio. |

---

## 7. Field Notes *(always visible)*

| Field | JSON key | Notes |
|-------|----------|-------|
| **Field Notes** | `fieldNotes` | Free text, **one note per line**. Saved as a list only if non-empty. These are **internal/developer notes** — firmware versions tested, known quirks, why a `--set-conf` value is needed, etc. Not part of the day-to-day operating steps. |

---

## ID & Filename rules

- **ID** = `slugify(Vendor + "-" + Model)` — lowercase; parentheses removed;
  any run of non-alphanumeric characters becomes a single `-`; leading/trailing
  `-` trimmed. Example: `Icom` + `IC-705` → **`icom-ic-705`**.
- **Filename** = `*<id>*.json`, or **`<id>.bt.json`** if the Bluetooth section is
  enabled.
- **Editing + renaming:** if you change Vendor/Model so the ID changes, the editor
  deletes the old file and writes the new one (no orphan left behind).
- **Duplicate:** copies a profile, appends *"(Copy)"* to the Model, and regenerates
  the ID so it saves as a new file.

---

## Connection type (derived, not a field)

The radio list shows a connection type, inferred from the data:
- **Bluetooth** — a `bluetooth` block is present.
- **USB/CAT** — a `rigctrl` block with `id` ≠ `1`.
- **Audio Only** — no `rigctrl`, or `rigctrl.id == 1` (Hamlib Dummy).

---

## Validation rules enforced on Save

- **Vendor** and **Model** are required.
- If **CAT Control** is enabled, **Hamlib Rig ID** is required.
- Empty optional fields are stripped from the saved JSON (clean files).

---

## Example saved profile

```json
{
  "id": "bg2fx-fx-4cr",
  "vendor": "BG2FX",
  "model": "FX-4CR",
  "bands": ["HF"],
  "rigctrl": {
    "id": "2031",
    "ptt": "RIG",
    "baud": "115200",
    "conf": "data_bits=8,serial_handshake=None,stop_bits=1,rts_state=OFF"
  },
  "audio": { "script": "/opt/emcomm-tools/conf/radios.d/audio/bg2fx-fx4cr.sh" },
  "notes": [
    "Toggle [SSB] button until both USB and DIG appear",
    "Set radio volume to 50",
    "Set alsamixer PCM to 21"
  ],
  "fieldNotes": [
    "Tested with FX-4CR firmware 1.7.3 (2025/09/01)",
    "Force RTS low (rts_state=OFF) or audio out with PTT fails"
  ]
}
```

---

## Appendix — Figuring out the CAT (rigctrl) settings for a new rig

The editor does **not** auto-discover these values, but LiaisonOS ships tools that
do. Recommended order when adding a new radio:

### 1. Find the **Hamlib Rig ID**
Hamlib's own model list is the source of truth:
```
rigctl -l | grep -i icom        # filter by vendor
rigctl -l | grep -i "705"       # or by model
```
The first column is the number you put in **Hamlib Rig ID**. (`1` = Dummy /
no-CAT; `2` = NET rigctl, used internally for testing.)

### 2. Find the **serial adapter and port** — `et-usb-diag`
```
et-usb-diag
```
Identifies the USB-serial chip (CP210x, CH340, FTDI FT232R/FT-X, STM32 VCP…),
lists `/dev/ttyUSB*`, shows what `/dev/et-cat` points to, and **probes all
`/dev/ttyUSB*` for CAT**. This tells you *which port* the radio is on.
(emcomm-tools uses the stable symlink `/dev/et-cat`, so you don't hard-code
`ttyUSB0`.)

### 3. See what **baud / PTT** a model supports — Hamlib caps
```
rigctl -m <ID> --dump-caps | less
```
Shows the rig's supported serial speeds and PTT types. Set **Baud Rate** to match
the radio's own menu setting; pick **PTT Method** from what it supports (usually
`RIG` for a modern CAT rig).

### 4. Start from a known-good profile
If a similar rig already exists in the list, use **Duplicate** — the stock
`radios.d` profiles are a library of working id/baud/ptt/conf combos. Often the
fastest path.

### 5. **Test** that your settings actually talk — `et-diag-rigctld`
After saving your profile and making it **active**:
```
et-diag-rigctld
```
It restarts `rigctld` with your profile's `id / baud / ptt / conf` in full Hamlib
debug, tries to read the frequency, and saves a log to `~/Documents/`. If it reads
a sane frequency back, your CAT settings are correct.

You can also test **any candidate before saving** (exactly what the diag does
internally): start rigctld, then query it:
```
rigctld -m <ID> -r /dev/et-cat -s <BAUD> -P <PTT>      # terminal 1
rigctl -m 2 -r 127.0.0.1:4532 f                        # terminal 2 — prints freq?
```
A valid frequency back = the ID, baud and port are right.

### Troubleshooting map (symptom → likely field)
| Symptom | Most likely cause |
|---|---|
| rigctld connects but `f` returns an error/garbage | wrong **Baud** (sweep 9600 / 19200 / 38400 / 115200) or wrong **Hamlib ID** |
| "device not found" / no `/dev/et-cat` | wrong **port** — run `et-usb-diag`, check the adapter is enumerated |
| CAT reads fine but radio never transmits | wrong **PTT Method** (try `RIG`, else `RTS`/`DTR` per adapter) |
| Audio out only works intermittently with PTT | a serial-line quirk — set **--set-conf**, e.g. `rts_state=OFF` (the FX-4CR case) |
| CAT flaky but you can still key TX | enable **PTT Only Mode** |

> **Now built into the editor (LiaisonOS 2.4.5+):** the two hardest steps are
> in the CAT Control section itself —
> - **Find…** beside Hamlib Rig ID runs `rigctl -l` and fills the ID from a
>   vendor/model search (step 1 above, no terminal needed).
> - **Test CAT** reads the live frequency from the already-running rigctld
>   (step 5 above) — green + frequency = the engaged CAT is responding. It does
>   not spawn its own rigctld, matching how LiaisonOS keeps CAT engaged whenever
>   the rig is connected.
>
> The command-line steps below still work and are useful for deeper diagnosis
> (`--dump-caps`, `et-usb-diag`, `et-diag-rigctld`).

---

## Glossary — terms & abbreviations

| Term | Stands for | What it is |
|------|-----------|------------|
| **CAT** | Computer Aided Transceiver | Computer control of the radio (frequency, mode, PTT) over a serial/USB link. |
| **CI-V** | Communication Interface V | Icom's CAT protocol. Other makers have their own (Yaesu/Kenwood CAT). |
| **PTT** | Push To Talk | Keying the transmitter (switching the radio from receive to transmit). |
| **VOX** | Voice Operated eXchange | Radio transmits automatically when it detects audio — no PTT wiring needed. |
| **RTS** | Request To Send | An RS-232 serial **control line**; ham interfaces wire it to key PTT. |
| **DTR** | Data Terminal Ready | The other RS-232 serial control line, also used to key PTT. |
| **CM108** | (chip name) | A USB-audio chip whose GPIO pin is commonly used to key PTT (cheap USB dongles, DigiRig-style). |
| **Hamlib** | Ham Radio Control Libraries | The open-source library of radio drivers; provides `rigctl` / `rigctld` and the model-number list. |
| **rigctld** | rig control **daemon** | Hamlib's background service (TCP on :4532) that apps talk to; in LiaisonOS it runs whenever the rig is connected. |
| **rigctl** | rig control (CLI) | Hamlib's command-line client. `rigctl -l` lists model numbers; `rigctl -m <ID> --dump-caps` shows a model's capabilities. |
| **Hamlib Rig ID** | — | The numeric model number from `rigctl -l` (e.g. `3085` = Icom IC-705; `1` = Dummy/no-CAT). Goes in the **Hamlib Rig ID** field. |
| **TNC** | Terminal Node Controller | A packet-radio modem (hardware or software). |
| **KISS** | Keep It Simple, Stupid | A minimal framing protocol between a computer and a TNC (used by the Bluetooth TNC radios). |
| **VARA FM** | — | A soundcard data modem (Windows app, run under Wine in LiaisonOS) for FM packet/Winlink. |
| **Wine COM port** | — | A Windows serial-port name (e.g. `COM10`) as seen by apps under Wine, mapped to a host serial device. In LiaisonOS, **`COM10` → `/dev/et-cat`** (the connected rig/DigiRig). |
| **Maidenhead grid** | — | The amateur grid-square locator (e.g. `FN35`), derived from lat/lon. |
| **Baud rate** | — | Serial-link speed (bits/sec); must match the radio's menu setting. |
| **`--set-conf`** | — | Hamlib option for advanced per-rig serial tweaks (e.g. `rts_state=OFF`). |

---

*LiaisonOS — Radio Configuration field reference. Generated from `et-radio-config`
(editor.html + et-radio-config.py), general overlay.*
