# onyx-miuicamera

MiuiCamera `6.7.000500.0_IRedDragonICY` (Xiaomi 17 Ultra `nezha` base) for POCO F7 (`onyx`).

- Telegram: https://t.me/ireddragonicy_archive
- License: GPL-3.0-or-later (`LICENSE`)

## What's inside

- `port/java/` — Java mod sources (`OnyxPortHelper`, `ZoomModSettingsFragment`, `ProModSettingsFragment`) compiled via `javac` -> `d8` -> `baksmali` with dynamic R8 symbol remapping.
- `port/config/port_profile.json` — Feature toggles & compat flags (50MP buffer fix, Ultra HDR off when HDR off, Leica CV/Essential/Master Lens, 34 watermarks + brand/custom name switcher, Smooth Zoom Mod, Camera Quality & Pro Mod, HAL-unsupported modes disabled).
- `port/tools/` — Patch, build, sign, ROM install, and ADB deploy scripts.
- `sepolicy/vendor/` — Camera app, HAL, and HEIC Codec2 SELinux rules.
- `shims/` & `patches/patch_micamera_policy.py` — MIVI2 `libgui` shim and HAL policy abort guard.

## Build & Install

Requires JDK 11+, Python 3.10+, Android SDK (`d8`, `zipalign`, `apksigner`), and `apktool.jar` (in `port/tools/apktool.jar` or `APKTOOL_JAR`).

```bash
# Build from stock nezha APK and copy signed APK into vendor/xiaomi/onyx-miuicamera
python3 port/tools/build_and_sign.py --apk /path/to/MiuiCamera.apk --install-rom

# Build directly from a nezha OTA zip
python3 port/tools/build_and_sign.py --ota /path/to/nezha-ota.zip --install-rom

# Fast rebuild after editing Java/config + install to ROM + deploy via ADB
python3 port/tools/build_and_sign.py --reuse-decompiled --install-rom --install-adb --launch

# Revert ADB overlay back to ROM APK
python3 port/tools/deploy_adb.py --revert
```
