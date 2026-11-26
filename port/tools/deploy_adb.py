#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""
ADB Live Deploy, Cache Reset, and Diagnostic Runner for Nezha -> Onyx MiuiCamera Port.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

PORT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APK = PORT_ROOT / "out" / "MiuiCamera_IRedDragonICY-signed.apk"


def adb(*args, check=True, capture=False):
    cmd = ["adb", *args]
    print(f"[adb] $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=check, capture_output=capture, text=True)


def main():
    ap = argparse.ArgumentParser(description="Deploy & test patched MiuiCamera on connected onyx device")
    ap.add_argument("--apk", type=Path, default=DEFAULT_APK)
    ap.add_argument("--clear-data", action="store_true", help="Clear com.android.camera app data (recommended on major version upgrade)")
    ap.add_argument("--clear-wm-cache", action="store_true", help="Clear stale /data/vendor/camera/watermarks cache")
    ap.add_argument("--launch", action="store_true", help="Launch MiuiCamera and capture 8s diagnostic logcat")
    ap.add_argument("--revert", action="store_true", help="Revert system update back to ROM built-in MiuiCamera")
    args = ap.parse_args()

    if args.revert:
        adb("shell", "am force-stop com.android.camera", check=False)
        adb("shell", "pm uninstall-system-updates com.android.camera", check=False)
        print("[adb] Reverted to ROM built-in MiuiCamera.")
        return

    if not args.apk.exists():
        sys.exit(f"APK not found: {args.apk}")

    adb("shell", "am force-stop com.android.camera", check=False)

    if args.clear_wm_cache:
        adb("shell", "rm -rf /data/vendor/camera/watermarks /data/user/0/com.android.camera/files/watermarks /data/user/0/com.android.camera/files/video_watermarks", check=False)

    # Install over system priv-app (same platform signing key, higher versionCode)
    adb("install", "-r", "-d", "-g", str(args.apk))

    if args.clear_data:
        adb("shell", "pm clear com.android.camera", check=False)
        # Re-grant core runtime permissions for seamless test
        for perm in (
            "android.permission.CAMERA",
            "android.permission.RECORD_AUDIO",
            "android.permission.ACCESS_FINE_LOCATION",
            "android.permission.ACCESS_COARSE_LOCATION",
            "android.permission.READ_MEDIA_IMAGES",
            "android.permission.READ_MEDIA_VIDEO",
            "android.permission.READ_MEDIA_AUDIO",
            "android.permission.POST_NOTIFICATIONS",
        ):
            adb("shell", f"pm grant com.android.camera {perm}", check=False)

    # Print installed version info
    res = adb("shell", "dumpsys package com.android.camera | grep -E 'versionName|versionCode|codePath|signatures'", capture=True)
    print(res.stdout, flush=True)

    if args.launch:
        adb("logcat", "-c", check=False)
        adb("shell", "am start -n com.android.camera/.Camera")
        time.sleep(6)
        log = adb(
            "logcat",
            "-d",
            "-v",
            "time",
            "AndroidRuntime:E",
            "DEBUG:F",
            "OnyxPortHelper:V",
            "WatermarkUtils:V",
            "WmGalleryPreference:V",
            "LimitationUtil:V",
            "DataItemFeature:V",
            "CloudFilterUtils:V",
            "MiCamHAL:E",
            "Camera3-Device:E",
            "*:S",
            capture=True,
            check=False,
        )
        log_path = PORT_ROOT / "out" / "adb_launch_logcat.txt"
        log_path.write_text(log.stdout, encoding="utf-8")
        print(f"[adb] Captured diagnostic logcat ({len(log.stdout.splitlines())} lines) -> {log_path}")
        print(log.stdout[-4000:])


if __name__ == "__main__":
    main()
