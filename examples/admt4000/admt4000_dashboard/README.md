# ADMT4000 Dashboard (Flutter)

A dark dashboard app for the ADMT4000 — live angle/turns/temperature/fault
monitoring plus Calibration, Magnetic Reset, Motor Move, and Power-down
actions — for Windows desktop and Android. All hardware access happens in
Python; the Flutter app only talks to `../admt4000_bridge.py` over
HTTP/WebSocket. No hardware access code runs in Dart.

```
ADMT4000 (serial/COM) <--libiio--> admt4000_bridge.py (FastAPI + WebSocket)
        |
        v  JSON over HTTP/WS on LAN
Flutter app (Windows desktop + Android)
  - Windows: bridge runs on the same PC, app connects to 127.0.0.1
  - Android: bridge runs on a PC on the same Wi-Fi, phone connects by IP
```

The app has 5 tabs (bottom navigation): **Live** (read-only dashboard) and
**Calibration** / **Magnetic Reset** / **Motor Move** / **Power-down**
(actions, one of the Tkinter app's 5 example scripts each). Only one action
can run at a time, and the Live tab's stream is automatically paused while
an action runs (both would otherwise fight over the same serial port) and
resumes once it finishes.

## Prerequisites

- **Python 3** with this repo's `adi` package importable (run from a venv
  where `pyadi-iio` / `pylibiio` are installed, as usual for this repo).
- **Flutter SDK** (stable channel) on your PATH. Verify with:
  ```bash
  flutter doctor
  ```
  You need at minimum the Windows toolchain (Visual Studio with the
  "Desktop development with C++" workload) and/or the Android toolchain
  (Android Studio, for the Android SDK) depending on which target you build.
- The ADMT4000 connected via USB serial (or networked IIO), e.g. `COM10` on
  Windows.

## 1. Start the Python bridge

This step is required for **both** platforms — the Flutter app shows no
data and actions fail until this is running.

From `examples/admt4000/`:

```bash
pip install -r requirements_bridge.txt
python admt4000_bridge.py --uri serial:COM10,230400,8n1 --port 8765
```

- `--uri` is the same IIO context URI used by the other example scripts
  (`serial:<port>,<baud>,8n1` for USB, or `ip:<address>` for Ethernet).
- `--host 0.0.0.0` (the default) binds on all interfaces so the bridge is
  reachable from other devices on the LAN — required for Android. On
  Windows only, you could restrict this to `127.0.0.1` if you never plan to
  connect from a phone.
- If no hardware is attached yet, the bridge still starts and serves
  `/status`/`/stream` with a connection-error message; connect the hardware
  and `POST /connect` (or just restart the bridge) once it's plugged in.
- Leave this running in its own terminal/window for as long as you're using
  the app.

## 2. Run the app on Windows

```bash
cd examples/admt4000/admt4000_dashboard
flutter pub get
flutter run -d windows
```

This opens `admt4000_dashboard.exe` in a window. On the connection screen,
enter:
- **Host**: `127.0.0.1` (bridge running on the same PC)
- **Port**: `8765` (or whatever you passed to `--port`)

Useful keys while `flutter run` is attached: `r` hot reload, `R` hot
restart, `q` quit.

To build a standalone executable instead of running the dev session:

```bash
flutter build windows
```

Output: `build\windows\x64\runner\Release\admt4000_dashboard.exe`.

## 3. Run the app on Android

The bridge must run on a PC that's on the **same Wi-Fi network** as the
Android device (phone or emulator) — the app reaches it over the LAN, not
USB.

```bash
cd examples/admt4000/admt4000_dashboard
flutter pub get
flutter run -d android   # with a device plugged in (USB debugging on) or an emulator running
```

Find the bridge PC's LAN IP (Windows: `ipconfig`, look for the IPv4 address
on your Wi-Fi adapter — e.g. `192.168.1.23`). On the app's connection
screen:
- **Host**: that IP (e.g. `192.168.1.23`)
- **Port**: `8765`

To build an installable APK instead of running the dev session:

```bash
flutter build apk
```

Output: `build/app/outputs/flutter-apk/app-release.apk` — copy it to the
phone and install, or `flutter install` with a device connected.

> The app's `AndroidManifest.xml` enables cleartext (plain `http`/`ws`)
> traffic since the bridge isn't served over TLS — that's expected for a
> same-network local tool and already configured, no extra setup needed.

## Troubleshooting

- **"Waiting for data..." / `--` placeholders forever**: the bridge isn't
  running, isn't reachable at the host/port you entered, or hasn't
  connected to the hardware yet — check the bridge's terminal output and
  `curl http://<host>:<port>/status`.
- **Android can't reach the bridge**: confirm the phone and the bridge PC
  are on the same Wi-Fi network (not phone cellular data, not an isolated
  guest Wi-Fi), and that the bridge was started with `--host 0.0.0.0`
  (default) rather than `127.0.0.1`. Also check the PC's firewall isn't
  blocking the port.
- **An action tab's Run button does nothing / errors immediately**: another
  action may already be running on the bridge (only one at a time); wait
  for it to finish or restart the bridge.
- **Flutter/C++ build errors mentioning `dart:ui` types** (`Offset`, `Path`,
  `Size`, etc. "isn't a type") when running `flutter run -d windows`: this
  is a stale build cache, not an app bug. Fix with:
  ```bash
  flutter clean
  flutter pub get
  flutter run -d windows
  ```
