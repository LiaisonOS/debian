<h1 align="center">Liaison OS for Debian</h1>
<p align="center">
  <em>Explore. Connect. Respond.</em><br>
  <em>Explorer. Connecter. Répondre.</em>
</p>

<p align="center">
  <a href="https://liaisonos.com"><img src="https://img.shields.io/badge/Version-2.3.3-f59e0b?style=for-the-badge" alt="Version 2.3.3"></a>
  <a href="https://liaisonos.com/download"><img src="https://img.shields.io/badge/Download-ISO-22c55e?style=for-the-badge" alt="Download ISO"></a>
  <a href="https://opensource.org/licenses/MS-PL"><img src="https://img.shields.io/badge/License-Ms--PL-3b82f6?style=for-the-badge" alt="License Ms-PL"></a>
  <a href="https://va2ops.ca"><img src="https://img.shields.io/badge/Author-va2ops.ca-8b5cf6?style=for-the-badge" alt="Author"></a>
</p>

<p align="center">
  A bilingual (FR/EN) Debian-based Linux distribution purpose-built for amateur radio emergency communications.<br>
  Boot from USB, connect your radio, and you are on the air — with JS8Call, Pat-Winlink, VARA, VarAC, YAAC, fldigi, and a YAAC SAR plugin ready to go.
</p>

---

## Français | English

<table>
<tr>
<td width="50%" valign="top">

<h3>Français</h3>

<p>LiaisonOS (FR/EN) basée sur Debian pour radioamateurs.</p>

<p><strong>Mainteneur :</strong> <a href="https://va2ops.ca/fr/">Sylvain Deguire (VA2OPS)</a></p>

<p>
📖 <a href="FUTURES_fr.md">Documentation</a><br>
💾 <a href="https://liaisonos.com/download">Téléchargements ISO</a><br>
⚠️ <a href="DISCLAIMER.md">Avis de non-responsabilité</a>
</p>

</td>
<td width="50%" valign="top">

<h3>English</h3>

<p>LiaisonOS (EN/FR) Debian-based Linux for Ham Radio.</p>

<p><strong>Maintainer:</strong> <a href="https://va2ops.ca">Sylvain Deguire (VA2OPS)</a></p>

<p>
📖 <a href="FUTURES.md">Documentation</a><br>
💾 <a href="https://liaisonos.com/download">ISO Downloads</a><br>
⚠️ <a href="DISCLAIMER.md">Disclaimer</a>
</p>

</td>
</tr>
</table>

---

## ✨ What's New in 2.3.3

### 📧 QtPatWinlink — Pat Winlink, the LiaisonOS way

- **Fully native Qt app** — no browser tabs, no context switching. Pat Winlink reimagined as a LiaisonOS-first experience
- **Touch Mode from day one** — designed for 7" tablets and car-mount screens
- **Inbox / Outbox / Sent / Archive** — one-tap fetch, mark-as-read, message viewer with reply/forward
- **Double-tap-to-connect RMS table** — band / mode / distance filters, favourites, last-selected memory
- **Session console** — live progress bar, smart Abort / Disconnect button, VARA & Mercury retry handling

### 📝 Pat Forms — Embedded

- **Full Pat Forms catalog** rendered inside the app via an embedded web view — pick, fill, send
- **Position reports** — submit your current GPS location to the active Winlink session in one tap

### ⚙️ et-supervisor — Hardened & Snappy

- **SIGCHLD-driven death detection** — dashboard updates the instant a modem closes (no more half-stuck modes)
- **Early state broadcast** — eliminates the "had to click Stop twice" lag
- **Zombie process handling** — closing VARA HF/FM now reliably ends the active mode
- **Thread-safety pass** — RLock-based state, cancellable waits, full reactor model

### 🔗 Mode Chains, Refined

- **QtPatWinlink in the chain** — each Winlink mode launches QtPatWinlink as a chain step; close the app, the whole mode shuts down cleanly
- **JS8Spotter ignore-exit** — JS8Spotter joins the JS8Call chain without taking JS8Call down when dismissed

## ✨ What's New in 2.3.2 / 2.3.1

### 🖥️ QtDashboard — Native C++ Dashboard

- **Fully native C++ Qt5 application** — replaces the Python/GTK et-dashboard. Faster, leaner, purpose-built for LiaisonOS
- **Embedded UTC clock with seconds** — no lag, always visible in the top bar
- **Inline modem selector** — choose your modem directly below the active mode, no popup or menu diving
- **Radio/CAT and user profile** — configurable from the dashboard itself
- **6 recent modes** — always ready to relaunch in one tap

### 📱 Desktop & Touch Mode

- **Dual UI mode** — Desktop mode for mouse/keyboard, Touch mode for 7" tablets and car-mount screens
- **Instant switching** — toggle between modes with no restart required, stored in `user.json`
- **Platform feature** — supported apps adapt automatically. et-repeater already includes a full Touch Mode card grid with slide-out filter drawer and live search. More apps to follow.

### 🖧 QtTerminal — Native Terminal (v2.3.2)

LiaisonOS introduced its own terminal app to host Paracon, Chattervox and LinBPQ — better control over modes that still need a terminal interface.

## ✨ What's New in 2.3.0

### 🛰️ GPS Sync & QtGpsSync

- **GPS Sync button** in the dashboard — sync system time in one tap from a Bluetooth Android device running the LiaisonGPS app, or from a serial GPS receiver (Kenwood TH-D74, TH-D75)
- **QtGpsSync rebuilt in C++** — connects over Bluetooth in two modes: *Sync* sets system time and Maidenhead grid square, *GPS* provides a continuous live feed

### 📡 Mercury HF Modem — Beta

Mercury v2 (HERMES/Rhizomatica, sponsored by ARDC) included as beta. Client apps connect via VARA protocol name — same client protocol, Mercury modem underneath.

## ✨ What's New in 2.2.7 (1)

### 🛰️ GPS — Redesigned, Event-Driven

- **Event-driven architecture** — The dashboard GPS engine is completely rebuilt on a Unix socket notification system. No more polling timer or flag file — state changes are instant and reliable
- **Visual GPS states** — The dashboard GPS indicator now shows green (active with fix), yellow (active, no data), and red (inactive), with a pulse blink on each GPS update

## ✨ What's New in 2.2.6

### 🛰️ Bluetooth GPS Sync

- **Sync from Android** — Install the LiaisonGPS app on any Android phone and sync your system clock and Maidenhead grid square via Bluetooth — no internet required
- **Sync from Kenwood radio** — Kenwood TH-D74 and TH-D75 radios broadcast NMEA frames via Bluetooth natively — no app required

### 📡 Bluetooth &amp; WiFi Save/Restore

- **Bluetooth pairings** — Paired devices are saved with USB persistence and restored automatically on next boot
- **WiFi credentials** — Known networks are saved and reconnect automatically after every boot

## ✨ What's New in 2.2.3

### 🏕️ Bug Fix — Overlay Scripts

A script error in v2.2.2 corrupted several overlay files (wrapper-rigctld.sh, wrapper-gpsd.sh, et-audio, et-log, radio audio profiles, and others), breaking rigctld, gpsd and et-supervisor on startup. All affected files have been restored.

## ✨ What's New in 2.2.2

### 🏕️ Pat Winlink — Updated to v1.0.0

New field logging application for Parks on the Air and Summits on the Air activations.

- **Updated to v1.0.0** — Latest upstream release of the Pat Winlink client

### 📡 QtTermTCP — BBS Session Cache

- **BBS session cache** — Sessions with packet BBS nodes are now cached locally by callsign using SQLite. Bulletin lists and message bodies are stored automatically during your session
- **Offline browsing** — Browse cached bulletins and read messages anytime via the BBS Cache dialog, even without a radio connection

---

## 📻 Key Features

| Feature | Description |
|---------|-------------|
| 🌐 **Bilingual** | French and English with language selection at first boot |
| 📧 **Pat-Winlink** | Email over RF with VARA HF/FM and ARDOP |
| 💬 **JS8Call / VarAC** | HF keyboard messaging and chat |
| 📡 **APRS (YAAC)** | Position tracking with SAR plugin for field evidence marking |
| 📟 **BBS / Packet** | LinBPQ server, QtTermTCP, Paracon clients |
| 🔭 **WSJT-X / fldigi** | FT8, FT4, PSK, RTTY, CW and more |
| 🏕️ **POTA/SOTA Logger** | Built-in field logger with ADIF export |
| 📻 **Repeater Directory** | Offline browser with RepeaterBook import and one-click radio programming |
| 🛰️ **GPS Tracking** | Real-time grid square updates with repeater distance refresh |
| 🗺️ **Offline Maps** | Navit navigation + MBTile server for offline mapping |
| 📖 **Offline Reference** | Kiwix with Wikipedia and encyclopedia ZIM files |
| 🔌 **24+ Radios** | Icom, Yaesu, Kenwood, Xiegu, Elecraft, QRP Labs and more |
| 💾 **USB Persistence** | Complete environment saved and restored across reboots |
| 🖥️ **Web Dashboard** | One-click mode launch with 16 operational modes |

---

## 📥 Quick Start

1. **Download** the ISO from [SourceForge](https://sourceforge.net/projects/emcomm-tools/files/ISO/)
2. **Write** to USB with [balenaEtcher](https://etcher.balena.io) or [Ventoy](https://ventoy.net)
3. **Boot** from USB — select your language (FR/EN)
4. **Configure** your callsign and radio
5. **Operate** — select a mode from the dashboard and you're on the air

> **Tip:** Use [Ventoy](https://ventoy.net) for multi-boot USB with a separate data partition for maps and Wikipedia files. See the [full README](https://sourceforge.net/projects/emcomm-tools/files/ISO/README.md) on SourceForge for the complete Ventoy guide.

---

## 📻 Supported Radios

### USB Direct

| Vendor | Model | Notes |
|--------|-------|-------|
| BG2FX | FX-4CR | USB CAT + audio |
| Icom | IC-705, IC-7100, IC-7200, IC-7300, IC-9700 | USB CAT + audio |
| QRP Labs | QMX | USB CAT |
| Xiegu | X6100 | USB CAT + audio |
| Yaesu | FT-710, FT-891, FT-991A, FTX-1 | USB CAT + audio (varies) |

### Via DigiRig Interface

Elecraft KX-2, Lab599 TX-500MP, Xiegu G90, Yaesu FT-818ND, FT-857D, FT-897D, and any radio with DigiRig Mobile or Lite.

### Bluetooth KISS TNC

Kenwood TH-D74, Kenwood TH-D75, VGC VR-N76, BTECH UV-PRO

---

## 🏗️ Operational Modes

The dashboard provides **16 one-click operational modes:**

| Category | Modes |
|----------|-------|
| **Winlink** | VARA HF, VARA FM, Packet, ARDOP |
| **Chat** | JS8Call, VarAC, Fldigi, Chattervox, Chattervox BT |
| **BBS** | Paracon, QtTermTCP, BBS Server (LinBPQ) |
| **APRS** | YAAC Client, YAAC BT, APRS Digipeater |
| **Other** | FT8/FT4 (WSJT-X), Direwolf KISS TNC |

---

## 🔗 Links

| | |
|---|---|
| 💾 **ISO Downloads** | [SourceForge](https://sourceforge.net/projects/emcomm-tools/files/ISO/) |
| 🐙 **GitHub** | [LiaisonOS/debian](https://github.com/liaisonos/debian/) |
| 👤 **Maintainer** | [va2ops.ca](https://va2ops.ca) |
| 📧 **Contact** | <a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="c5acaba3aa85a0a8a6aaa8a8e8b1aaaaa9b6eba6a4">[email&#160;protected]</a> |

---

## 🙏 Acknowledgments

- **The Debian Ham Radio Team** — Maintaining excellent ham radio packages
- **José Alberto Nieto Ros (EA5HVK)** — VARA HF/FM modem development
- **Irad Deutsch (4Z1AC) and the VarAC Development Team** — VarAC
- **Andrew Pavlin (KA2DDO)** — YAAC (Yet Another APRS Client)
- **Martin Hebnes Pedersen (LA5NTA)** — Pat Winlink
- **John Wiseman (G8BPQ)** — linBPQ / QtTermTCP
- **Martin F N Cooper** — Paracon
- **David Freese (W1HKJ)** — fldigi
- **Joe Taylor (K1JT) and the WSJT Development Team** — WSJT-X
- **Gaston Gonzalez (KT7RUN)** — Initial fork from Original EmComm-Tools OS Community project

---

## 📄 License

This project is a derivative work of EmComm-Tools OS Community, licensed under the **[Microsoft Public License (Ms-PL)](https://opensource.org/licenses/MS-PL)**. In compliance with Ms-PL Section 3(C), we retain all copyright, patent, trademark, and attribution notices from the origina
