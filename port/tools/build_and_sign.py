#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""
End-to-End Reusable Build, Sign, ROM-Install & ADB-Deploy Pipeline
for Onyx MiuiCamera (_IRedDragonICY / Nezha 6.7 base).

Usage examples:
  # Build from a stock MiuiCamera.apk
  python3 tools/build_and_sign.py --apk /path/to/MiuiCamera.apk

  # Build directly from a Xiaomi 17 Ultra (nezha) full-OTA .zip
  python3 tools/build_and_sign.py --ota /path/to/nezha-ota_full-OS4.x.zip

  # Fast rebuild from cached decompiled baseline + install straight into ROM vendor tree
  python3 tools/build_and_sign.py --reuse-decompiled --install-rom

  # Fast rebuild + live deploy to connected POCO F7 via ADB
  python3 tools/build_and_sign.py --reuse-decompiled --install-adb --launch
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PORT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SDK_BUILD_TOOLS = Path("/opt/android-sdk/build-tools/36.0.0")
FALLBACK_ROM_ROOT = Path("/home/ireddragonicy/Data/projects/onyx/los_workspace/src")


def detect_rom_root(explicit: Path = None) -> Path | None:
    if explicit and explicit.is_dir():
        return explicit
    for par in (PORT_ROOT, *PORT_ROOT.parents):
        if (par / "build" / "target" / "product" / "security" / "platform.pk8").is_file():
            return par
    if (FALLBACK_ROM_ROOT / "build" / "target" / "product" / "security" / "platform.pk8").is_file():
        return FALLBACK_ROM_ROOT
    return None


def run(cmd, **kw):
    print(f"[build] $ {' '.join(str(c) for c in cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kw)


def find_apktool(explicit: Path = None) -> Path:
    candidates = []
    if explicit:
        candidates.append(explicit)
    env_jar = os.environ.get("APKTOOL_JAR")
    if env_jar:
        candidates.append(Path(env_jar))
    candidates.extend([
        PORT_ROOT / "tools" / "apktool.jar",
        PORT_ROOT / "apktool.jar",
        Path("/home/ireddragonicy/Data/projects/onyx/nezha-camera-port/tools/apktool.jar"),
        Path("/home/ireddragonicy/Data/projects/onyx/miuicamera_patchwork/apktool.jar"),
        Path("/usr/share/java/apktool.jar"),
        Path("/usr/share/apktool/apktool.jar"),
    ])
    for c in candidates:
        if c.is_file():
            return c
    raise FileNotFoundError("apktool.jar not found! Set APKTOOL_JAR, place in tools/apktool.jar, or pass --apktool")


def find_sdk_tool(name: str) -> Path:
    cand = DEFAULT_SDK_BUILD_TOOLS / name
    if cand.is_file():
        return cand
    w = shutil.which(name)
    if w:
        return Path(w)
    bt = Path("/opt/android-sdk/build-tools")
    if bt.is_dir():
        for d in sorted(bt.iterdir(), reverse=True):
            c = d / name
            if c.is_file():
                return c
    raise FileNotFoundError(f"Android SDK tool '{name}' not found!")


def main():
    ap = argparse.ArgumentParser(description="Build, patch, align, sign, and optionally install MiuiCamera APK")
    ap.add_argument("--ota", type=Path, help="Nezha full-OTA .zip or payload.bin to extract first")
    ap.add_argument("--apk", type=Path, help="Input stock MiuiCamera.apk")
    ap.add_argument("--reuse-decompiled", action="store_true", help="Skip apktool d; patch work/patched_nezha from clean baseline")
    ap.add_argument("--profile", type=Path, default=PORT_ROOT / "config" / "port_profile.json")
    ap.add_argument("--apktool", type=Path, default=None)
    ap.add_argument("--rom-root", type=Path, default=None, help="Android/LineageOS source root (auto-detected)")
    ap.add_argument("--key", type=Path, default=None, help="Platform signing key (.pk8)")
    ap.add_argument("--cert", type=Path, default=None, help="Platform signing cert (.x509.pem)")
    ap.add_argument("--out", type=Path, default=PORT_ROOT / "out" / "MiuiCamera_IRedDragonICY-signed.apk")
    ap.add_argument("--install-rom", action="store_true", help="Copy final signed APK into vendor/xiaomi/onyx-miuicamera")
    ap.add_argument("--install-adb", action="store_true", help="Deploy final signed APK to connected device via ADB")
    ap.add_argument("--clear-data", action="store_true", help="(with --install-adb) Clear com.android.camera data")
    ap.add_argument("--launch", action="store_true", help="(with --install-adb) Launch camera & collect logcat")
    ap.add_argument("--no-strict", action="store_true", help="Allow continuing even if an optional patch fails")
    args = ap.parse_args()

    rom_root = detect_rom_root(args.rom_root)
    sec_dir = (rom_root / "build" / "target" / "product" / "security") if rom_root else None
    key_path = args.key or (sec_dir / "platform.pk8" if sec_dir else None)
    cert_path = args.cert or (sec_dir / "platform.x509.pem" if sec_dir else None)
    if not (key_path and key_path.is_file() and cert_path and cert_path.is_file()):
        raise FileNotFoundError("Platform signing key/cert not found! Pass --key and --cert or --rom-root")

    apktool = find_apktool(args.apktool)
    zipalign = find_sdk_tool("zipalign")
    apksigner = find_sdk_tool("apksigner")

    apk_path = args.apk
    if args.ota:
        ota_stem = args.ota.stem.replace(" ", "_")
        src_dir = PORT_ROOT / "source" / ota_stem
        run([sys.executable, PORT_ROOT / "tools" / "extract_ota.py", args.ota, "-o", src_dir])
        apk_path = src_dir / "product" / "priv-app" / "MiuiCamera" / "MiuiCamera.apk"

    clean_dec = PORT_ROOT / "work" / "decompiled_nezha"
    patch_dir = PORT_ROOT / "work" / "patched_nezha"

    if not args.reuse_decompiled:
        if not apk_path:
            cands = sorted((PORT_ROOT / "source").glob("*/product/priv-app/MiuiCamera/MiuiCamera.apk"))
            if not cands:
                ap.error("Provide --apk or --ota (no extracted MiuiCamera.apk found in source/)")
            apk_path = cands[-1]
        print(f"[build] Source APK: {apk_path}", flush=True)
        run(["java", "-Xmx6g", "-jar", apktool, "d", "-f", "-o", clean_dec, apk_path])

    if not clean_dec.exists():
        raise FileNotFoundError(f"Clean decompiled directory missing: {clean_dec}")

    print(f"[build] Preparing clean working copy at {patch_dir} ...", flush=True)
    if patch_dir.exists():
        shutil.rmtree(patch_dir)
    subprocess.run(["cp", "-a", "--reflink=auto", str(clean_dec), str(patch_dir)], check=True)

    report_path = PORT_ROOT / "out" / "patch_report.json"
    patch_cmd = [
        sys.executable,
        PORT_ROOT / "tools" / "patch_camera.py",
        patch_dir,
        "-c",
        args.profile,
        "--report",
        report_path,
    ]
    if not args.no_strict:
        patch_cmd.append("--strict")
    run(patch_cmd)

    out_dir = args.out.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    unsigned_apk = PORT_ROOT / "work" / "MiuiCamera-unsigned.apk"
    aligned_apk = PORT_ROOT / "work" / "MiuiCamera-aligned.apk"
    for f in (unsigned_apk, aligned_apk):
        if f.exists():
            f.unlink()

    run(["java", "-Xmx6g", "-jar", apktool, "b", "--use-aapt2", "-o", unsigned_apk, patch_dir])
    run([zipalign, "-f", "-p", "4", unsigned_apk, aligned_apk])

    run([
        apksigner, "sign",
        "--key", key_path,
        "--cert", cert_path,
        "--v1-signing-enabled", "true",
        "--v2-signing-enabled", "true",
        "--v3-signing-enabled", "true",
        "--out", args.out,
        aligned_apk,
    ])
    run([apksigner, "verify", "--verbose", args.out])

    sz_mb = args.out.stat().st_size / (1024 * 1024)
    print(f"\n[build] SUCCESS! Final signed APK ({sz_mb:.1f} MB): {args.out}", flush=True)

    if args.install_rom:
        if not rom_root:
            raise RuntimeError("--install-rom requested, but ROM root could not be detected (pass --rom-root)")
        vendor_apk = rom_root / "vendor" / "xiaomi" / "onyx-miuicamera" / "proprietary" / "system" / "priv-app" / "MiuiCamera" / "MiuiCamera.apk"
        vendor_apk.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.out, vendor_apk)
        print(f"[build] Installed APK into ROM vendor tree -> {vendor_apk}", flush=True)

    if args.install_adb:
        adb_cmd = [sys.executable, PORT_ROOT / "tools" / "deploy_adb.py", "--apk", args.out, "--clear-wm-cache"]
        if args.clear_data:
            adb_cmd.append("--clear-data")
        if args.launch:
            adb_cmd.append("--launch")
        run(adb_cmd)


if __name__ == "__main__":
    main()
