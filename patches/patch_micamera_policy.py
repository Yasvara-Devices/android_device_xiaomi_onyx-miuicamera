#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch libmicamera_hal_policy.so: don't abort when MiviSettings lookup fails.

Without this, any photo mode whose fwkOpMode is absent from
/odm/etc/camera/xiaomi/mivivendorstreamconfig.json (e.g. legacy 0x8002/0x8003
emitted by ported MIUI camera mods) makes
policy::DeviceSessionPolicy::buildVendorConfiguration() call
__android_log_assert() -> SIGABRT -> vendor.qti.camera.provider dies ->
"can't connect camera".

Patch (8 bytes @ .text 0x43f0c, valid for md5 cde1d9ea... generation):
  mov w5, w22 ; bl __android_log_assert
  -> ldr x21, [x20, #136] ; b 0x43f14
i.e. the failure path now rejoins the "config not found" continuation
(x21 restored from this+136), exactly like the "cbz x0, 0x43f14" path.

Usage: patch_micamera_policy.py <path-to-libmicamera_hal_policy.so>
Idempotent: exits 0 with a notice if already patched.
Fails loudly (exit 1) if the expected pattern is not found, so blob updates
never silently ship an unpatched lib.
"""
import struct, sys, hashlib

EXPECT_MD5_PREFIX = "cde1d9ea"   # generation this offset/pattern was verified on
OFF = 0x43F0C
OLD = struct.pack("<II", 0x2A1603E5, 0x9402B1B2)   # mov w5,w22 ; bl log_assert
NEW = struct.pack("<II", 0xF9404695, 0x14000001)   # ldr x21,[x20,#136] ; b +1


def main(path: str) -> int:
    data = bytearray(open(path, "rb").read())
    cur = bytes(data[OFF:OFF + 8])

    if cur == NEW:
        print(f"[patch-policy] {path}: already patched")
        return 0

    if cur != OLD:
        md5 = hashlib.md5(data).hexdigest()
        print(f"[patch-policy] ERROR: pattern mismatch at {hex(OFF)}\n"
              f"  found : {cur.hex()}\n"
              f"  expect: {OLD.hex()}\n"
              f"  md5   : {md5} (verified generation: {EXPECT_MD5_PREFIX}...)\n"
              f"  The vendor blob changed — re-derive the abort site:\n"
              f"    1. find 'get InternalStreamConfigInfo failed' string vaddr\n"
              f"    2. find adrp+add ref in buildVendorConfiguration\n"
              f"    3. patch the __android_log_assert call to restore x21\n"
              f"       and branch to the not-found join point")
        return 1

    data[OFF:OFF + 8] = NEW
    open(path, "wb").write(data)
    md5 = hashlib.md5(data).hexdigest()
    print(f"[patch-policy] {path}: patched (new md5 {md5})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
