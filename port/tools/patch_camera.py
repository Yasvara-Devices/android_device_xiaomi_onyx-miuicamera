#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Self-Healing, Pattern-Based MiuiCamera Port Patcher (Nezha -> Onyx).

Designed to survive Xiaomi MiuiCamera source updates (6.7 -> 6.8 -> 7.x+) where
R8 renames classes/methods and rotates XOR keys on com.mi.device.* classes.
ZERO hardcoded obfuscated class/method names: every target is discovered
dynamically via immutable strings, JacocoIgnore keys, R$string field names,
and XOR-decrypted com.mi.device.* class hierarchy.
"""

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
from collections import defaultdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers: XOR device decoding & Smali indexing
# ---------------------------------------------------------------------------

def decode_device_cls(obf: str) -> str:
    s = obf.strip("L;")
    if not s or ord(s[0]) < 128:
        return s
    key = ord(s[0]) ^ ord("c")
    dec = "".join(chr(ord(ch) ^ key) for ch in s)
    return dec if dec.startswith("com.") else f"?({obf})"


class SmaliWorkspace:
    """Lazy-cached index over a decompiled MiuiCamera directory."""

    def __init__(self, root: Path):
        self.root = root
        self.smali_files = sorted(root.rglob("*.smali"))
        self._text_cache = {}
        self.modified_paths = set()
        self.obf_to_dec = {}
        self.dec_to_obf = {}
        self.dec_to_path = {}
        self.cls_super = {}
        self.cls_methods = {}
        self.common_obf = None
        self.wrapper_cls = None
        self.wrapper_path = None
        self.r_string = {}
        self.jacoco_map = defaultdict(set)  # key -> set of ("C"|"W", "name(args)ret")
        self._discover_core()

    def read(self, p: Path) -> str:
        if p not in self._text_cache:
            self._text_cache[p] = p.read_text(encoding="utf-8", errors="replace")
        return self._text_cache[p]

    def write(self, p: Path, content: str):
        self._text_cache[p] = content
        self.modified_paths.add(p)
        p.write_text(content, encoding="utf-8")

    def find_files_containing(self, *needles: str, skip_obf: bool = True):
        hits = []
        for p in self.smali_files:
            if skip_obf and any(ord(c) > 127 for c in p.name):
                continue
            txt = self.read(p)
            if all(n in txt for n in needles):
                hits.append(p)
        return hits

    def _discover_core(self):
        # 1. Decode all obfuscated com.mi.device.* classes
        for p in self.smali_files:
            if not any(ord(c) > 127 for c in p.name):
                continue
            txt = self.read(p)
            mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", txt, re.M)
            if not mc:
                continue
            obf = mc.group(1)
            dec = decode_device_cls(obf)
            ms = re.search(r"^\.super (L[^;\s]+;)", txt, re.M)
            self.obf_to_dec[obf] = dec
            self.dec_to_obf[dec] = obf
            self.dec_to_path[dec] = p
            self.cls_super[obf] = ms.group(1) if ms else ""
            methods = {}
            for mm in re.finditer(r"^\.method[^\n]*? (\S+?)\(([^)]*)\)(\S+)\n(.*?)^\.end method", txt, re.M | re.S):
                methods[f"{mm.group(1)}({mm.group(2)}){mm.group(3)}"] = mm.group(4)
            self.cls_methods[obf] = methods

        self.common_obf = self.dec_to_obf.get("com.mi.device.common.Common")
        if not self.common_obf:
            raise RuntimeError("Could not discover com.mi.device.common.Common class!")

        # 2. Discover DataItemFeature wrapper: declares an instance field of type common_obf AND delegates >80 calls to it
        field_pat = re.compile(r"^\.field[^\n]*:" + re.escape(self.common_obf) + r"\s*$", re.M)
        for p in self.smali_files:
            if any(ord(c) > 127 for c in p.name) or p.stat().st_size < 35000:
                continue
            txt = self.read(p)
            if self.common_obf in txt and field_pat.search(txt) and txt.count(f"{self.common_obf}->") > 80:
                mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", txt, re.M)
                if mc:
                    self.wrapper_cls = mc.group(1)
                    self.wrapper_path = p
                    break
        if not self.wrapper_cls:
            raise RuntimeError("Could not discover DataItemFeature wrapper class!")

        # 3. Load R$string map (find any R$string class containing pref_camera_leica_limited)
        for p in self.smali_files:
            if p.stat().st_size < 100000:
                continue
            txt = self.read(p)
            if "pref_camera_leica_limited:I = 0x" in txt:
                for m in re.finditer(r"\.field public static final (\w+):I = (0x[0-9a-fA-F]+)", txt):
                    self.r_string[m.group(1)] = int(m.group(2), 16)
                break

        # 4. Build JacocoIgnore key -> Common/Wrapper methods index
        ann_pat = re.compile(r"\.method[^\n]*\n(.*?)\.end method", re.S)
        jkey_pat = re.compile(r'key = "([^"]+)"')
        call_c = re.compile(re.escape(self.common_obf) + r"->(\w+\([^)]*\)\S+)")
        call_w = re.compile(re.escape(self.wrapper_cls) + r"->(\w+\([^)]*\)\S+)")
        for p in self.smali_files:
            if any(ord(c) > 127 for c in p.name):
                continue
            txt = self.read(p)
            if "JacocoIgnore" not in txt:
                continue
            for m in ann_pat.finditer(txt):
                body = m.group(1)
                if "JacocoIgnore" not in body:
                    continue
                km = jkey_pat.search(body)
                if not km:
                    continue
                k = km.group(1)
                head = body[:1400]
                for cm in call_c.findall(head):
                    self.jacoco_map[k].add(("C", cm))
                for wm in call_w.findall(head):
                    self.jacoco_map[k].add(("W", wm))

    def _device_chain(self, dec_name: str):
        obf = self.dec_to_obf.get(dec_name)
        chain = []
        while obf and obf in self.cls_super:
            chain.append(obf)
            obf = self.cls_super[obf]
        return chain

    def eval_bool_on_device(self, dec_name: str, method_sig: str):
        """Return True/False if simple const or instance-of bool getter, else None."""
        chain = self._device_chain(dec_name)
        chain_set = set(chain)
        for c in chain:
            body = self.cls_methods.get(c, {}).get(method_sig)
            if body is None:
                continue
            lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith((".", "#", ":"))]
            if len(lines) == 2 and lines[1].startswith("return"):
                m = re.match(r"const(?:/4|/16)?\s+[pv]\d+,\s*(-?0x[0-9a-fA-F]+|-?\d+)", lines[0])
                if m:
                    return bool(int(m.group(1), 0))
                m = re.match(r"instance-of\s+[pv]\d+,\s*p0,\s*(L[^;\s]+;)", lines[0])
                if m:
                    return m.group(1) in chain_set
            if len(lines) == 3 and lines[0].startswith("instance-of") and lines[1].startswith("xor-int/lit8"):
                m = re.match(r"instance-of\s+[pv]\d+,\s*p0,\s*(L[^;\s]+;)", lines[0])
                if m:
                    return not (m.group(1) in chain_set)
            # Complex (e.g. Nezha->i5()Z delegating to Te/b.V())
            return None
        return None

    def is_nezha_only_bool(self, method_sig: str) -> bool:
        """True if Onyx evaluates False and Nezha evaluates True (or complex non-False)."""
        o = self.eval_bool_on_device("com.mi.device.Onyx", method_sig)
        n = self.eval_bool_on_device("com.mi.device.Nezha", method_sig)
        return (o is False) and (n is True or n is None)

    def resolve_wrapper_to_common(self, wrapper_method: str):
        """Given a wrapper method like 'm2()Z', find Common methods it calls."""
        txt = self.read(self.wrapper_path)
        name, sig = wrapper_method.split("(", 1)
        sig = "(" + sig
        m = re.search(
            rf"^\.method[^\n]* {re.escape(name)}{re.escape(sig)}\n(.*?)^\.end method",
            txt,
            re.M | re.S,
        )
        if not m:
            return []
        return re.findall(re.escape(self.common_obf) + r"->(\w+\([^)]*\)\S+)", m.group(1))

    def find_common_call_near_anchor(self, anchor: str, sig_suffix: str = "()Z", file_hint: str = None, window: int = 1600, nezha_only: bool = False):
        """Find Common->method(sig_suffix) invoked near an immutable anchor string/class."""
        call_c = re.compile(re.escape(self.common_obf) + r"->(\w+" + re.escape(sig_suffix) + r")")
        call_w = re.compile(re.escape(self.wrapper_cls) + r"->(\w+" + re.escape(sig_suffix) + r")")
        found = []
        for p in self.smali_files:
            if any(ord(c) > 127 for c in p.name):
                continue
            if file_hint and file_hint not in str(p):
                continue
            txt = self.read(p)
            if anchor not in txt:
                continue
            for am in re.finditer(re.escape(anchor), txt):
                win = txt[max(0, am.start() - window) : min(len(txt), am.end() + window)]
                for cm in call_c.findall(win):
                    if (not nezha_only or self.is_nezha_only_bool(cm)) and cm not in found:
                        found.append(cm)
                for wm in call_w.findall(win):
                    for cm in self.resolve_wrapper_to_common(wm):
                        if cm.endswith(sig_suffix) and (not nezha_only or self.is_nezha_only_bool(cm)) and cm not in found:
                            found.append(cm)
        return found


# ---------------------------------------------------------------------------
# Group A: AOSP / LineageOS / Android 17 Compatibility Patches
# ---------------------------------------------------------------------------

def patch_compat_global_mod_device(ws: SmaliWorkspace):
    """Force mod_device '_global' check to true in all matching classes (prevents HyperOS SecurityCenter crash)."""
    hits = ws.find_files_containing('"ro.product.mod_device"', '"_global"', "Ljava/lang/String;->contains(")
    patched = []
    for p in hits:
        txt = ws.read(p)
        pat = re.compile(
            r'(const-string v\d+, "_global"\s+invoke-virtual \{v\d+, v\d+\}, Ljava/lang/String;->contains\(Ljava/lang/CharSequence;\)Z\s+)move-result (v\d+)(\s+sput-boolean \2, L[^;]+;->\w+:Z)'
        )
        m = pat.search(txt)
        if m:
            reg = m.group(2)
            new_txt = txt[: m.start()] + m.group(1) + f"const/4 {reg}, 0x1" + m.group(3) + txt[m.end() :]
            ws.write(p, new_txt)
            patched.append(str(p.relative_to(ws.root)))
        elif re.search(r'"_global"\s+invoke-virtual[^\n]+contains[^\n]+\s+const/4 v\d+, 0x1\s+sput-boolean', txt):
            patched.append(str(p.relative_to(ws.root)) + "(already)")
    if patched:
        return True, f"Forced global mod_device in {', '.join(patched)}"
    return False, "Anchor ro.product.mod_device + _global not found"


def patch_compat_disable_safe_check(ws: SmaliWorkspace):
    """Force camera.debug.safe.check.disable static boolean to true (bypasses anti-tamper)."""
    hits = ws.find_files_containing('"camera.debug.safe.check.disable"')
    for p in hits:
        txt = ws.read(p)
        pat = re.compile(
            r'(const-string (v\d+), "camera\.debug\.safe\.check\.disable"\s+invoke-static \{\2, v\d+\}, L[^;]+;->\w+\(Ljava/lang/String;Z\)Z\s+)move-result (v\d+)(\s+sput-boolean \3, L[^;]+;->\w+:Z)'
        )
        m = pat.search(txt)
        if m:
            reg = m.group(3)
            new_txt = txt[: m.start()] + m.group(1) + f"const/4 {reg}, 0x1" + m.group(4) + txt[m.end() :]
            ws.write(p, new_txt)
            return True, f"Disabled integrity gate in {p.relative_to(ws.root)}"
        if re.search(r'"camera\.debug\.safe\.check\.disable"[\s\S]{0,160}const/4 v\d+, 0x1\s+sput-boolean', txt):
            return True, f"Already patched in {p.relative_to(ws.root)}"
    return False, "Anchor camera.debug.safe.check.disable not found"


def patch_compat_obfu_res_fallback(ws: SmaliWorkspace):
    """Wrap obfu_res/ AssetManager.open() in FileNotFoundException -> ResourcesCompat.getDrawable."""
    hits = ws.find_files_containing('"obfu_res/"', "getValueForDensity", "AssetManager;->open")
    for p in hits:
        txt = ws.read(p)
        if ":try_start_obfu" in txt or "ResourcesCompat;->getDrawable" in txt:
            return True, f"Already patched in {p.relative_to(ws.root)}"
        # Locate the exact method containing getValueForDensity + obfu_res/
        mpat = re.compile(
            r"(\.method public static \w+\(Landroid/content/res/Resources;ILjava/lang/Integer;\)Landroid/graphics/drawable/Drawable;\n)(.*?)(\.end method)",
            re.S,
        )
        for m in mpat.finditer(txt):
            body = m.group(2)
            if "getValueForDensity" not in body or '"obfu_res/"' not in body:
                continue
            lm = re.search(r"^\s*\.locals (\d+)", body, re.M)
            if not lm:
                continue
            orig_locals = int(lm.group(1))
            save_reg = f"v{orig_locals}"
            new_body = re.sub(r"^(\s*\.locals )\d+", rf"\g<1>{orig_locals + 1}", body, count=1, flags=re.M)
            # Save p1 (original resId) right before getValueForDensity
            gvd_pat = re.compile(r"(\s+)(invoke-virtual \{p0, p1, p2, v\d+, v\d+\}, Landroid/content/res/Resources;->getValueForDensity\(IILandroid/util/TypedValue;Z\)V)")
            if not gvd_pat.search(new_body):
                continue
            new_body = gvd_pat.sub(rf"\1move {save_reg}, p1\n\n\1\2", new_body, count=1)
            # Wrap AssetManager;->open
            open_pat = re.compile(r"(\s+)(invoke-virtual \{p2, p1\}, Landroid/content/res/AssetManager;->open\(Ljava/lang/String;\)Ljava/io/InputStream;\s+move-result-object p2)")
            if not open_pat.search(new_body):
                continue
            new_body = open_pat.sub(
                r"\1:try_start_obfu\n\1\2\n\1:try_end_obfu\n\1.catch Ljava/io/FileNotFoundException; {:try_start_obfu .. :try_end_obfu} :catch_obfu",
                new_body,
                count=1,
            )
            # Append catch handler right before .end method (use framework Resources.getDrawable — never R8-obfuscated!)
            catch_block = (
                "\n    :catch_obfu\n"
                "    const/4 p2, 0x0\n\n"
                f"    invoke-virtual {{p0, {save_reg}, p2}}, Landroid/content/res/Resources;->getDrawable(ILandroid/content/res/Resources$Theme;)Landroid/graphics/drawable/Drawable;\n\n"
                "    move-result-object p0\n\n"
                "    return-object p0\n"
            )
            new_body = new_body + catch_block
            new_txt = txt[: m.start()] + m.group(1) + new_body + m.group(3) + txt[m.end() :]
            ws.write(p, new_txt)
            return True, f"Added obfu_res fallback in {p.relative_to(ws.root)}"
    return False, "Anchor obfu_res/ + getValueForDensity not found"


def patch_compat_stub_jcodec(ws: SmaliWorkspace):
    """Stub jcodec MP4UtilEx post-processing helper (prevents MP4 save issues)."""
    hits = ws.find_files_containing('"MP4UtilEx"', "Ljava/io/File;")
    target_ref = None
    for p in hits:
        txt = ws.read(p)
        m = re.search(r"invoke-static \{[^}]+\}, (L[^;]+;)->(\w+)\(Ljava/io/File;L[^;]+;\)V", txt)
        if m:
            target_ref = (m.group(1), m.group(2))
            break
    if not target_ref:
        return False, "Could not locate jcodec helper via MP4UtilEx"
    cls_desc, meth_name = target_ref
    cls_rel = cls_desc.strip("L;") + ".smali"
    target_path = None
    for p in ws.smali_files:
        if str(p).endswith("/" + cls_rel):
            target_path = p
            break
    if not target_path:
        return False, f"jcodec class file {cls_rel} not found"
    txt = ws.read(target_path)
    mpat = re.compile(
        rf"(\.method public static {re.escape(meth_name)}\(Ljava/io/File;L[^;]+;\)V\n)(.*?)(\.end method)",
        re.S,
    )
    m = mpat.search(txt)
    if not m:
        return False, f"Method {meth_name} not found in {target_path.relative_to(ws.root)}"
    stub = "    .locals 0\n\n    return-void\n"
    ws.write(target_path, txt[: m.start()] + m.group(1) + stub + m.group(3) + txt[m.end() :])
    return True, f"Stubbed jcodec {cls_desc}->{meth_name}() in {target_path.relative_to(ws.root)}"


def patch_compat_skip_nal_length(ws: SmaliWorkspace):
    """Skip NAL length-prefixed encoder output tags (fixes AOSP MediaCodec video recording)."""
    hits = ws.find_files_containing('"feature-nal-length-bitstream"', '"nal-length-in-bytes"')
    for p in hits:
        txt = ws.read(p)
        # Look for the guard `if-eqz vX, :cond_Y` right before the block that sets vendor.x.feature-nal-length-bitstream
        idx = txt.find('"feature-nal-length-bitstream"')
        pre = txt[max(0, idx - 700) : idx]
        # Find the last `iget-boolean vX, vY, ... ; if-eqz vX, :cond_Y` right before the NAL block
        m_list = list(re.finditer(r"iget-boolean (v\d+), (v\d+), L[^;]+;->\w+:Z\s+(if-eqz \1, (:\w+))", pre))
        if m_list:
            m = m_list[-1]
            abs_start = max(0, idx - 700) + m.start(3)
            abs_end = max(0, idx - 700) + m.end(3)
            label = m.group(4)
            new_txt = txt[:abs_start] + f"goto {label}" + txt[abs_end:]
            ws.write(p, new_txt)
            return True, f"Skipped NAL length-prefixed block in {p.relative_to(ws.root)} (goto {label})"
        if re.search(r"iget-boolean v\d+, v\d+, L[^;]+;->\w+:Z\s+goto :\w+", pre):
            return True, f"Already patched in {p.relative_to(ws.root)}"
    return False, "Anchor feature-nal-length-bitstream not found"


def patch_compat_google_photos_gallery(ws: SmaliWorkspace):
    """Force GalleryHelper to open Google Photos via REVIEW intent instead of missing MiuiGallery."""
    hits = ws.find_files_containing('"GalleryHelper"', '"com.android.camera.action.REVIEW"')
    for p in hits:
        txt = ws.read(p)
        # Pattern: sget-boolean vX, LTe/c;->m:Z ... if-eqz vX, :cond_0 ... invoke-static {}, L...;->c()Z ; move-result vX ; if-eqz vX, :cond_0
        pat = re.compile(
            r"(sget-boolean (v\d+), L[^;]+;->\w+:Z)(\s+const/4 v\d+, 0x1\s+const-string v\d+, \"GalleryHelper\"[\s\S]{0,160}if-eqz \2, :\w+\s+)(invoke-static \{\}, L[^;]+;->\w+\(\)Z\s+move-result \2)(\s+if-eqz \2, :\w+)"
        )
        m = pat.search(txt)
        if m:
            reg = m.group(2)
            repl = f"const/4 {reg}, 0x1" + m.group(3) + f"const/4 {reg}, 0x1" + m.group(5)
            ws.write(p, txt[: m.start()] + repl + txt[m.end() :])
            return True, f"Forced Google Photos REVIEW branch in {p.relative_to(ws.root)}"
        if re.search(r'const/4 (v\d+), 0x1\s+const/4 v\d+, 0x1\s+const-string v\d+, "GalleryHelper"', txt):
            return True, f"Already patched in {p.relative_to(ws.root)}"
    return False, "Anchor GalleryHelper + REVIEW not found"


def patch_compat_fresh_thumbnail(ws: SmaliWorkspace):
    """Add android:query-arg-match-pending=1 to MediaStore thumbnail query."""
    hits = ws.find_files_containing('"android:query-arg-limit"', "ContentResolver;->query(")
    for p in hits:
        txt = ws.read(p)
        if '"android:query-arg-match-pending"' in txt:
            return True, f"Already patched in {p.relative_to(ws.root)}"
        # Find the Bundle.putInt for android:query-arg-limit right before ContentResolver.query
        pat = re.compile(
            r'(invoke-virtual \{(v\d+), (v\d+), (v\d+)\}, Landroid/os/Bundle;->putInt\(Ljava/lang/String;I\)V)(\s+invoke-virtual \{[^}]+\}, Landroid/content/ContentResolver;->query\()'
        )
        # Only patch the one inside the method that has "android:query-arg-limit"
        for mm in re.finditer(r"\.method[^\n]+\n(.*?)\.end method", txt, re.S):
            body = mm.group(1)
            if '"android:query-arg-limit"' not in body or "ContentResolver;->query(" not in body:
                continue
            m = pat.search(body)
            if not m:
                continue
            bundle_reg, key_reg, val_reg = m.group(2), m.group(3), m.group(4)
            # Check what value val_reg holds (it holds 1 for limit=1, which equals MATCH_INCLUDE=1!)
            inject = (
                f"{m.group(1)}\n\n"
                f'    const-string {key_reg}, "android:query-arg-match-pending"\n\n'
                f"    invoke-virtual {{{bundle_reg}, {key_reg}, {val_reg}}}, Landroid/os/Bundle;->putInt(Ljava/lang/String;I)V"
                f"{m.group(5)}"
            )
            new_body = body[: m.start()] + inject + body[m.end() :]
            ws.write(p, txt[: mm.start(1)] + new_body + txt[mm.end(1) :])
            return True, f"Injected MATCH_PENDING into thumbnail query in {p.relative_to(ws.root)}"
    return False, "Anchor android:query-arg-limit + ContentResolver.query not found"


def patch_compat_leica_limited_toast(ws: SmaliWorkspace):
    """Suppress pref_camera_leica_limited popup if present."""
    res_id = ws.r_string.get("pref_camera_leica_limited")
    if not res_id:
        return True, "No pref_camera_leica_limited resource id (not needed)"
    hex_id = hex(res_id)
    hits = ws.find_files_containing(hex_id)
    patched = []
    for p in hits:
        if "R$" in p.name or ws.read(p).count(".field public static final") > 100:
            continue
        txt = ws.read(p)
        pat = re.compile(rf"(\s*const [pv]\d+, {re.escape(hex_id)}\s+invoke-static \{{[^}}]+\}}, L[^;]+;->\w+\(Landroid/content/Context;IZ\)V)")
        if pat.search(txt):
            ws.write(p, pat.sub("\n    nop", txt))
            patched.append(str(p.relative_to(ws.root)))
    if patched:
        return True, f"Suppressed Leica limited popup in {patched}"
    return True, f"No active toast caller for {hex_id} in this build (clean)"


# ---------------------------------------------------------------------------
# Group B: Leica / Leitzphone / LCC Edition & Cloud Filters
# ---------------------------------------------------------------------------

def patch_leitz_force_lcc_theme(ws: SmaliWorkspace):
    """Force Leitzphone/LCC customize check (Te/b.V() -> true & Va/c theme_customize default -> 'LCC')."""
    details = []
    # 1. Force Te/b.V()Z (contains "LCC" and returns bool) to return 1
    wtxt = ws.read(ws.wrapper_path)
    mpat = re.compile(r"(\.method public static (\w+)\(\)Z\n)(.*?)(\.end method)", re.S)
    found_v = False
    for m in mpat.finditer(wtxt):
        body = m.group(3)
        if '"LCC"' in body and "equals(Ljava/lang/Object;)Z" in body:
            stub = "    .locals 1\n\n    const/4 v0, 0x1\n\n    return v0\n"
            wtxt = wtxt[: m.start()] + m.group(1) + stub + m.group(4) + wtxt[m.end() :]
            ws.write(ws.wrapper_path, wtxt)
            details.append(f"{ws.wrapper_cls}->{m.group(2)}()Z = true")
            found_v = True
            break
    if not found_v:
        # Check if already patched by checking a 4-line LCC method or warn
        return False, f'Could not find static LCC theme check in {ws.wrapper_path.relative_to(ws.root)}'

    # 2. In Va/c.smali (holds ro.boot.product.theme_customize and ro.theme_customize),
    # default empty string fallback to "LCC" so both fields are always "LCC" when prop is unset!
    hits = ws.find_files_containing('"ro.boot.product.theme_customize"', '"ro.theme_customize"')
    for p in hits:
        txt = ws.read(p)
        changed = False
        for prop in ("ro.boot.product.theme_customize", "ro.theme_customize"):
            pat = re.compile(
                rf'(const-string (v\d+), "{re.escape(prop)}"\s+)invoke-static \{{\2, (v\d+)\}}, (L[^;]+;->\w+\(Ljava/lang/String;Ljava/lang/String;\)Ljava/lang/String;)'
            )
            m = pat.search(txt)
            if m:
                # Replace with: const-string vX, "LCC" ; invoke-static {vX, vY} -> or if empty, set "LCC"
                # Even cleaner: pass "LCC" as the default value arg! Wait: v4 might be reused for other props.
                # So instead: right after `sput-object v0, LVa/c;->u:Ljava/lang/String;`, overwrite if empty,
                # OR simply pass a literal "LCC" if we don't clobber v4! Wait: v0 is overwritten anyway,
                # so we can do:
                #   const-string v0, "ro.boot.product.theme_customize"
                #   invoke-static {v0, v4}, ...
                #   move-result-object v0
                #   invoke-virtual {v0}, Ljava/lang/String;->isEmpty()Z
                #   move-result v1  -> wait, avoid clobbering any live register!
                # Simplest & 100% register-safe: `const-string v0, "LCC"` -> `sput-object v0, ...`!
                pass
            pat2 = re.compile(
                rf'(const-string (v\d+), "{re.escape(prop)}"\s+invoke-static \{{\2, v\d+\}}, L[^;]+;->\w+\(Ljava/lang/String;Ljava/lang/String;\)Ljava/lang/String;\s+move-result-object \2\s+)(sput-object \2, L[^;]+;->\w+:Ljava/lang/String;)'
            )
            m2 = pat2.search(txt)
            if m2:
                reg = m2.group(2)
                repl = m2.group(1) + f'const-string {reg}, "LCC"\n\n    ' + m2.group(3)
                txt = txt[: m2.start()] + repl + txt[m2.end() :]
                changed = True
        if changed:
            ws.write(p, txt)
            details.append(f"Forced theme_customize='LCC' in {p.relative_to(ws.root)}")
            break

    # 3. Also force LUe/c$a;->b (reads ro.boot.product.theme_customize, empty on onyx) -> "lcc"
    ue_hits = ws.find_files_containing('"ro.boot.product.theme_customize"')
    for p in ue_hits:
        txt = ws.read(p)
        if '"ro.theme_customize"' in txt:
            continue
        pat_ue = re.compile(
            r'(const-string (v\d+), "ro\.boot\.product\.theme_customize"[\s\S]{0,220}?)sput-object \2, (L[^;]+;->\w+:Ljava/lang/String;)'
        )
        m_ue = pat_ue.search(txt)
        if m_ue and 'const-string v0, "lcc"\n\n    sput-object' not in txt:
            reg = m_ue.group(2)
            repl = m_ue.group(1) + f'const-string {reg}, "lcc"\n\n    sput-object {reg}, {m_ue.group(3)}'
            ws.write(p, txt[: m_ue.start()] + repl + txt[m_ue.end() :])
            details.append(f"Forced lowercase 'lcc' in {p.relative_to(ws.root)}")
            break

    return True, "; ".join(details)


def patch_leica_cloud_filter_device(ws: SmaliWorkspace):
    """Force DeviceHelper.isLeiCaDevice()=true and isNotLeiCaDevice()=false."""
    hits = [p for p in ws.smali_files if p.name == "DeviceHelper.smali" and "cloudfilter" in str(p)]
    if not hits:
        return False, "cloudfilter DeviceHelper.smali not found"
    p = hits[0]
    txt = ws.read(p)
    txt = re.sub(
        r"(\.method public static isLeiCaDevice\(\)Z\n).*?(\.end method)",
        r"\1    .locals 1\n\n    const/4 v0, 0x1\n\n    return v0\n\2",
        txt,
        count=1,
        flags=re.S,
    )
    txt = re.sub(
        r"(\.method public static isNotLeiCaDevice\(\)Z\n).*?(\.end method)",
        r"\1    .locals 1\n\n    const/4 v0, 0x0\n\n    return v0\n\2",
        txt,
        count=1,
        flags=re.S,
    )
    ws.write(p, txt)
    return True, f"Forced Leica device in {p.relative_to(ws.root)}"


def patch_leica_cloud_filter_custom_json(ws: SmaliWorkspace):
    """Always load cloudfilter/cloud_filter_custom.json (full Leica filter catalog)."""
    hits = ws.find_files_containing('"cloudfilter/cloud_filter_custom.json"', '"cloudfilter/cloud_filter_not_custom.json"')
    if not hits:
        return False, "CloudFilterUtils JSON selector not found"
    p = hits[0]
    txt = ws.read(p)
    new_txt = txt.replace('"cloudfilter/cloud_filter_not_custom.json"', '"cloudfilter/cloud_filter_custom.json"')
    ws.write(p, new_txt)
    return True, f"Forced cloud_filter_custom.json in {p.relative_to(ws.root)}"


# ---------------------------------------------------------------------------
# Group C: Watermark Unlocker, Missing Brand Asset Fixer, & Lag Optimizer
# ---------------------------------------------------------------------------

def patch_watermark_unlock_all(ws: SmaliWorkspace):
    """
    Neutralize artificial device/region/theme/poco watermark restrictions while
    PRESERVING filterByNameLength so short/long variants never duplicate.
    """
    disable_tags = [
        "filterByBuildRegion: ",
        "filterByDeviceType: ",
        "filterByTheme: ",
        "filterBySystemProperties: ",
        "filterCustomProperties: ",
    ]
    patched = []
    for tag in disable_tags:
        hits = ws.find_files_containing(f'"{tag}"', "LimitationUtil")
        if not hits:
            return False, f"Watermark validator with tag '{tag}' not found"
        p = hits[0]
        txt = ws.read(p)
        # Replace invoke(Ljava/lang/Object;)Ljava/lang/Object; to immediately return Boolean.FALSE
        mpat = re.compile(
            r"(\.method public final invoke\(Ljava/lang/Object;\)Ljava/lang/Object;\n)(.*?)(\.end method)",
            re.S,
        )
        m = mpat.search(txt)
        if not m:
            return False, f"invoke() not found in {p.relative_to(ws.root)}"
        stub = "    .locals 0\n\n    sget-object p0, Ljava/lang/Boolean;->FALSE:Ljava/lang/Boolean;\n\n    return-object p0\n"
        ws.write(p, txt[: m.start()] + m.group(1) + stub + m.group(3) + txt[m.end() :])
        patched.append(f"{tag.strip(': ')}({p.stem})")
    return True, "Unlocked watermarks: " + ", ".join(patched)


def _tint_webp_with_python(src_white: Path, dst_path: Path, hex_rgb: str) -> bool:
    """Create a color-tinted WebP from a white logo WebP preserving alpha (PIL or ffmpeg)."""
    r = int(hex_rgb[0:2], 16)
    g = int(hex_rgb[2:4], 16)
    b = int(hex_rgb[4:6], 16)
    try:
        from PIL import Image
        im = Image.open(src_white).convert("RGBA")
        px = im.load()
        w, h = im.size
        for y in range(h):
            for x in range(w):
                pr, pg, pb, pa = px[x, y]
                if pa > 0:
                    px[x, y] = (int(pr * r / 255), int(pg * g / 255), int(pb * b / 255), pa)
        im.save(dst_path, "WEBP", lossless=True)
        return True
    except ImportError:
        pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
        vf = f"format=rgba,colorchannelmixer=rr={rf:.4f}:gg={gf:.4f}:bb={bf:.4f}:aa=1"
        res = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", str(src_white), "-vf", vf, "-lossless", "1", str(dst_path)],
            capture_output=True,
        )
        return res.returncode == 0 and dst_path.exists()
    return False


def patch_watermark_fix_missing_poco_logos(ws: SmaliWorkspace):
    """Ensure every watermark template with xiaomi_<color>.webp has matching poco_<color>.webp."""
    wm_root = ws.root / "assets" / "watermarks"
    if not wm_root.exists():
        return False, "assets/watermarks directory not found"

    # Find a crisp reference poco_white.webp for tinting
    ref_poco_white = None
    for cand in sorted(wm_root.rglob("poco_white.webp")):
        ref_poco_white = cand
        break

    fixed = []
    for item_dir in sorted(wm_root.glob("*/*")):
        if not item_dir.is_dir():
            continue
        for xfile in sorted(item_dir.glob("xiaomi_*.webp")):
            suffix = xfile.name[len("xiaomi_") :]  # e.g. "E6A54F.webp"
            pfile = item_dir / ("poco_" + suffix)
            if pfile.exists():
                continue
            hex_m = re.match(r"^([0-9A-Fa-f]{6})\.webp$", suffix)
            if hex_m and ref_poco_white and _tint_webp_with_python(ref_poco_white, pfile, hex_m.group(1)):
                fixed.append(f"{item_dir.relative_to(wm_root)}/{pfile.name} (tinted #{hex_m.group(1)})")
            else:
                # Fallback to copying xiaomi asset so ImageView never breaks/renders blank
                shutil.copy2(xfile, pfile)
                fixed.append(f"{item_dir.relative_to(wm_root)}/{pfile.name} (fallback copy)")
    return True, f"Fixed {len(fixed)} missing POCO watermark logos: {fixed}"


def patch_watermark_fix_gallery_lag(ws: SmaliWorkspace):
    """
    Fix Watermark Settings page lag:
    1. Eliminate redundant main-thread LRg/M;->i(true) rescan inside WmGalleryPreference UI runnable.
    2. Downscale/optimize WmGalleryPreference serial blocking & prevent UI thread CountDownLatch stall.
    """
    notes = []

    # Fix 1: Find the UI runnable that calls LRg/M;->i(Z) with true (const/4 v2, 0x1) inside WmGalleryPreference
    hits = ws.find_files_containing(
        "Lcom/android/camera/fragment/watermark/wmSettingV2/WmGalleryPreference;->L0:",
        '"WmGalleryPreference"',
    )
    fixed_ui_rescan = False
    for p in hits:
        txt = ws.read(p)
        # Look for: invoke-virtual {v0, v2}, LRg/M;->i(Z)Ljava/util/List; where v2=1 and v3=0
        pat = re.compile(
            r"(invoke-static \{\}, Ljava/lang/System;->currentTimeMillis\(\)J\s+move-result-wide v\d+\s+invoke-virtual \{v\d+, )(v\d+)(\}, L[^;]+;->i\(Z\)Ljava/util/List;)"
        )
        m = pat.search(txt)
        if m:
            # Find a register holding 0 in the method prologue (const/4 vX, 0x0)
            # In A4/u.smali, v3 is const/4 v3, 0x0; let's detect whichever reg is 0!
            zero_m = re.search(r"const/4 (v\d+), 0x0", txt[: m.start()])
            if zero_m and zero_m.group(1) != m.group(2):
                zreg = zero_m.group(1)
                ws.write(p, txt[: m.start()] + m.group(1) + zreg + m.group(3) + txt[m.end() :])
                notes.append(f"Removed redundant main-thread watermark rescan in {p.relative_to(ws.root)}")
                fixed_ui_rescan = True
                break

    # Fix 2: In WmGalleryPreference.m0() (called on UI thread when toggling switch),
    # it ALSO calls LRg/M;->i(true) synchronously on the main thread! Change to i(false).
    wgp_hits = [p for p in ws.smali_files if p.name == "WmGalleryPreference.smali"]
    if wgp_hits:
        p = wgp_hits[0]
        txt = ws.read(p)
        pat_m0 = re.compile(
            r"(\.method public final m0\(\)V[\s\S]{0,400}const/4 (v\d+), )0x1(\s+invoke-virtual \{v\d+, \2\}, L[^;]+;->i\(Z\)Ljava/util/List;)"
        )
        if pat_m0.search(txt):
            ws.write(p, pat_m0.sub(r"\g<1>0x0\3", txt, count=1))
            notes.append(f"Removed main-thread rescan in WmGalleryPreference.m0()")

    # Fix 3: In WatermarkEffect draw runnable (WmSettingPreviewFragment),
    # avoid indefinite CountDownLatch.await() blocking the Main Looper — use 16ms timeout!
    cdl_hits = ws.find_files_containing('"WatermarkEffect"', "CountDownLatch;->await()V")
    for p in cdl_hits:
        txt = ws.read(p)
        # Replace `invoke-virtual {v4}, Ljava/util/concurrent/CountDownLatch;->await()V`
        # with `const-wide/16 v6, 0x10`? Wait, v6 is wide so check locals first; safer: check if sget-object TimeUnit.MILLISECONDS fits.
        # Even simpler & safer: `invoke-virtual {v4}, Ljava/util/concurrent/CountDownLatch;->await()V`
        # -> we can use `TimeUnit.MILLISECONDS` with 2-reg long only if free regs exist, OR post on background HandlerThread!
        # Let's check exact method locals in p!
        mpat = re.compile(r"(\.method public final run\(\)V\n\s+\.locals )(\d+)(.*?)(\.end method)", re.S)
        m = mpat.search(txt)
        if m and "CountDownLatch;->await()V" in m.group(3):
            locs = int(m.group(2))
            body = m.group(3)
            # Add 3 locals: v{locs}, v{locs+1} (wide 16L) and v{locs+2} (TimeUnit)
            r_lo = f"v{locs}"
            r_tu = f"v{locs + 2}"
            await_pat = re.compile(r"invoke-virtual \{(v\d+)\}, Ljava/util/concurrent/CountDownLatch;->await\(\)V")
            am = await_pat.search(body)
            if am and locs + 3 <= 15:
                latch_reg = am.group(1)
                repl = (
                    f"const-wide/16 {r_lo}, 0x10\n\n"
                    f"    sget-object {r_tu}, Ljava/util/concurrent/TimeUnit;->MILLISECONDS:Ljava/util/concurrent/TimeUnit;\n\n"
                    f"    invoke-virtual {{{latch_reg}, {r_lo}, {r_tu}}}, Ljava/util/concurrent/CountDownLatch;->await(JLjava/util/concurrent/TimeUnit;)Z"
                )
                new_body = body[: am.start()] + repl + body[am.end() :]
                ws.write(p, txt[: m.start()] + f"{m.group(1)}{locs + 3}{new_body}{m.group(4)}" + txt[m.end() :])
                notes.append(f"Capped WatermarkEffect MainLooper CountDownLatch wait to 16ms in {p.relative_to(ws.root)}")
                break

    if not notes:
        return False, "No watermark lag targets matched"
    return True, "; ".join(notes)


# ---------------------------------------------------------------------------
# Group C2: Dynamic OnyxPortHelper Runtime + 50MP Buffer + SmoothZoom + WM Brand Menu
# ---------------------------------------------------------------------------

def _discover_port_helper_symbols(ws: SmaliWorkspace) -> dict:
    """Dynamically discover all obfuscated AndroidX/Watermark/Wrapper symbols needed by OnyxPortHelper."""
    cached = getattr(ws, "_port_helper_syms", None)
    if cached is not None:
        return cached

    syms = {"wrapper_cls": ws.wrapper_cls}

    # 1. Preference.smali methods & fields
    pref_path = next((p for p in ws.smali_files if str(p).endswith("/androidx/preference/Preference.smali")), None)
    pg_path = next((p for p in ws.smali_files if str(p).endswith("/androidx/preference/PreferenceGroup.smali")), None)
    sb_path = next((p for p in ws.smali_files if str(p).endswith("/com/android/camera/fragment/settings/b.smali")), None)
    if not (pref_path and pg_path and sb_path):
        raise RuntimeError("Missing Preference / PreferenceGroup / settings/b smali")

    pref_txt = ws.read(pref_path)
    pg_txt = ws.read(pg_path)
    sb_txt = ws.read(sb_path)

    m_clk = re.search(r"^\.field[^\n]* (\w+):Landroidx/preference/Preference\$d;\s*$", pref_txt, re.M)
    for mm in re.finditer(r"^\.method[^\n]* (\w+)\(([^)]*)\)(\S+)\n(.*?)^\.end method", pref_txt, re.M | re.S):
        mname, margs, mret, mbody = mm.group(1), mm.group(2), mm.group(3), mm.group(4)
        if margs == "Ljava/lang/String;" and mret == "V" and "Preference does not have a key assigned." in mbody:
            syms["pref_set_key"] = mname
            m_kf = re.search(r"iput-object p1, p0, Landroidx/preference/Preference;->(\w+):Ljava/lang/String;", mbody)
            syms["pref_key_field"] = m_kf.group(1) if m_kf else "m"
        elif margs == "Ljava/lang/CharSequence;" and mret == "V" and "Preference already has a SummaryProvider set." in mbody:
            syms["pref_set_summary"] = mname
        elif margs == "Ljava/lang/String;" and mret == "V" and "Landroid/text/TextUtils;->equals" in mbody and ":Ljava/lang/CharSequence;" in mbody:
            syms["pref_set_title"] = mname

    if not (m_clk and "pref_set_key" in syms and "pref_set_summary" in syms and "pref_set_title" in syms):
        raise RuntimeError(f"Could not resolve Preference symbols: {syms}")
    syms["pref_onclick_field"] = m_clk.group(1)

    # Extract defaultValueField, persistentField, and PreferenceGroup.addPreference from settings/b.addCheckBoxPreference
    m_acb = re.search(
        r"\.method private addCheckBoxPreference\(Landroidx/preference/PreferenceGroup;Ljava/lang/String;ZI\)Landroidx/preference/CheckBoxPreference;\n(.*?)\.end method",
        sb_txt,
        re.S,
    )
    if not m_acb:
        raise RuntimeError("Could not inspect settings/b.addCheckBoxPreference")
    acb_body = m_acb.group(1)
    m_defv = re.search(r"iput-object [pv]\d+, v0, Landroidx/preference/Preference;->(\w+):Ljava/lang/Object;", acb_body)
    m_pers = re.search(r"iput-boolean [pv]\d+, v0, Landroidx/preference/Preference;->(\w+):Z", acb_body)
    m_addp = re.search(r"invoke-virtual \{p1, v0\}, Landroidx/preference/PreferenceGroup;->(\w+)\(Landroidx/preference/Preference;\)Z", acb_body)
    if not (m_defv and m_pers and m_addp):
        raise RuntimeError("Could not resolve Preference defaultValue/persistent/addPreference")
    syms["pref_default_field"] = m_defv.group(1)
    syms["pref_persistent_field"] = m_pers.group(1)
    syms["pg_add_pref"] = m_addp.group(1)

    for mm in re.finditer(r"^\.method[^\n]* (\w+)\(Ljava/lang/CharSequence;\)Landroidx/preference/Preference;\n(.*?)^\.end method", pg_txt, re.M | re.S):
        if "Key cannot be null" in mm.group(2):
            syms["pg_find_pref"] = mm.group(1)
            break
    if "pg_find_pref" not in syms:
        raise RuntimeError("Could not resolve PreferenceGroup.findPreference")

    # 2. WmGalleryPreference brand/device fields & Wrapper singleton/z/A/x
    wgp_path = next((p for p in ws.smali_files if p.name == "WmGalleryPreference.smali"), None)
    wgf_path = next((p for p in ws.smali_files if p.name == "WmGalleryFragment.smali"), None)
    if not (wgp_path and wgf_path):
        raise RuntimeError("Missing WmGalleryPreference or WmGalleryFragment")
    wgp_txt = ws.read(wgp_path)
    wgf_txt = ws.read(wgf_path)

    m_sing = re.search(
        rf"sget-object [pv]\d+, (L[^;]+;)->(\w+):{re.escape(ws.wrapper_cls)}[\s\S]{{0,80}}?"
        rf"invoke-virtual \{{[pv]\d+\}}, {re.escape(ws.wrapper_cls)}->(\w+)\(\)Ljava/lang/String;\s+move-result-object ([pv]\d+)\s+"
        rf"iput-object \4, p0, Lcom/android/camera/fragment/watermark/wmSettingV2/WmGalleryPreference;->(\w+):Ljava/lang/String;[\s\S]{{0,80}}?"
        rf"invoke-virtual \{{[pv]\d+\}}, {re.escape(ws.wrapper_cls)}->(\w+)\(\)Ljava/lang/String;\s+move-result-object ([pv]\d+)\s+"
        rf"iput-object \7, p0, Lcom/android/camera/fragment/watermark/wmSettingV2/WmGalleryPreference;->(\w+):Ljava/lang/String;",
        wgp_txt,
    )
    if not m_sing:
        raise RuntimeError("Could not resolve Wrapper singleton & WmGalleryPreference brand/device fields")
    syms["wrapper_holder_cls"] = m_sing.group(1)
    syms["wrapper_holder_field"] = m_sing.group(2)
    syms["wrapper_brand_m"] = m_sing.group(3)
    syms["wgp_brand_field"] = m_sing.group(5)
    syms["wrapper_device_m"] = m_sing.group(6)
    syms["wgp_device_field"] = m_sing.group(8)

    # Find brandArray method called by wrapper_brand_m inside wrapper_path
    wtxt = ws.read(ws.wrapper_path)
    m_bm = re.search(
        rf"^\.method[^\n]* {re.escape(syms['wrapper_brand_m'])}\(\)Ljava/lang/String;\n(.*?)^\.end method",
        wtxt,
        re.M | re.S,
    )
    m_xa = re.search(rf"{re.escape(ws.wrapper_cls)}->(\w+)\(\)\[Ljava/lang/String;", m_bm.group(1)) if m_bm else None
    if not m_xa:
        raise RuntimeError("Could not resolve Wrapper brandArray method")
    syms["wrapper_array_m"] = m_xa.group(1)

    # 3. WmManager class, fields & methods from WmGalleryFragment
    m_mgr = re.search(r"^\.field[^\n]* mWmManager:(L[^;]+;)\s*$", wgf_txt, re.M)
    if not m_mgr:
        raise RuntimeError("Could not resolve WmGalleryFragment.mWmManager type")
    syms["wm_mgr_cls"] = m_mgr.group(1)
    mgr_rel = syms["wm_mgr_cls"].strip("L;") + ".smali"
    mgr_path = next((p for p in ws.smali_files if str(p).endswith("/" + mgr_rel)), None)
    if not mgr_path:
        raise RuntimeError(f"Could not find {mgr_rel}")
    mgr_txt = ws.read(mgr_path)
    m_grp = re.search(r"^\.field public (\w+):Ljava/util/ArrayList;\s*$", mgr_txt, re.M)
    m_cur_fld = re.search(r"^\.field public (\w+):Lcom/xiaomi/cam/watermark/a;\s*$", mgr_txt, re.M)
    m_load = re.search(r"^\.method[^\n]* (\w+)\(Z\)Ljava/util/List;\s*$", mgr_txt, re.M)
    m_cur = re.search(r"^\.method[^\n]* (\w+)\(\)Lcom/xiaomi/cam/watermark/a;\s*$", mgr_txt, re.M)
    m_isvid = re.search(r"^\.method public abstract (\w+)\(\)Z\s*$", mgr_txt, re.M)
    m_init_fld = None
    for mm in re.finditer(r"^\.method[^\n]* \w+\(\)V\n(.*?)^\.end method", mgr_txt, re.M | re.S):
        if '"initData: E"' in mm.group(1):
            mi = re.search(
                rf"iget-boolean (v\d+), p0, {re.escape(syms['wm_mgr_cls'])}->(\w+):Z[\s\S]{{0,120}}?if-eqz \1, :\w+\s+monitor-exit p0\s+return-void",
                mm.group(1),
            )
            if mi:
                m_init_fld = mi.group(2)
                break
    if not (m_grp and m_cur_fld and m_load and m_cur and m_isvid and m_init_fld):
        raise RuntimeError("Could not resolve WmManager fields/methods (groups/cur_fld/init/load/cur/isvid)")
    syms["wm_mgr_groups_field"] = m_grp.group(1)
    syms["wm_mgr_cur_field"] = m_cur_fld.group(1)
    syms["wm_mgr_init_field"] = m_init_fld
    syms["wm_mgr_load_m"] = m_load.group(1)
    syms["wm_mgr_cur_m"] = m_cur.group(1)
    syms["wm_mgr_isvid_m"] = m_isvid.group(1)

    # 4. WatermarkItem.m0()V (sendDataToMivi)
    wma_path = next((p for p in ws.smali_files if str(p).endswith("/com/xiaomi/cam/watermark/a.smali")), None)
    wma_txt = ws.read(wma_path)
    syms["wm_item_mivi_m"] = None
    for mm in re.finditer(r"^\.method[^\n]* (\w+)\(\)V\n(.*?)^\.end method", wma_txt, re.M | re.S):
        if "sendDataToMivi: from path >: " in mm.group(2):
            syms["wm_item_mivi_m"] = mm.group(1)
            break
    if not syms["wm_item_mivi_m"]:
        raise RuntimeError("Could not resolve WatermarkItem sendDataToMivi method")

    # 5. WmDeviceCache (LW8/d;) & WmUtils (LW8/g;)
    w8d_hits = ws.find_files_containing('"WestCoast3"', f"{ws.wrapper_cls}->{syms['wrapper_brand_m']}()Ljava/lang/String;")
    if w8d_hits:
        dtxt = ws.read(w8d_hits[0])
        mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", dtxt, re.M)
        mf = re.search(rf"^\.field public static volatile (\w+):{re.escape(mc.group(1))}\s*$", dtxt, re.M) if mc else None
        if mc and mf:
            syms["wm_devcache_cls"] = mc.group(1)
            syms["wm_devcache_field"] = mf.group(1)

    w8g_hits = ws.find_files_containing('"initCloudWatermarkEnv: workingDir = "')
    if w8g_hits:
        gtxt = ws.read(w8g_hits[0])
        mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", gtxt, re.M)
        mm = None
        for m in re.finditer(r"^\.method public static (\w+)\(Landroid/content/Context;Z\)Z\n(.*?)^\.end method", gtxt, re.M | re.S):
            if "initCloudWatermarkEnv: workingDir = " in m.group(2):
                mm = m.group(1)
                break
        if mc and mm:
            syms["wm_utils_cls"] = mc.group(1)
            syms["wm_utils_init_m"] = mm

    # 6. ZoomMod symbols: ZoomManager (k9/l), ZoomProtocol (Y6/d), Module interface (X), CameraManager (m6/l), Caps (n9/f.B)
    zm_hits = ws.find_files_containing('"setZoomRangeWithUI(): zoomRange = "')
    if not zm_hits:
        raise RuntimeError("Could not locate ZoomManager (setZoomRangeWithUI)")
    zm_txt = ws.read(zm_hits[0])
    mc_zm = re.search(r"^\.class[^\n]+(L[^;\s]+;)", zm_txt, re.M)
    syms["zm_cls"] = mc_zm.group(1)
    syms["zm_path"] = zm_hits[0]
    m_wr = re.search(
        r"^\.field public final (\w+):Ljava/lang/ref/WeakReference;\n\s+\.annotation[^\n]*\n\s+value = \{\s+\"Ljava/lang/ref/WeakReference<\",\s+\"(L[^;\s]+;)\"",
        zm_txt,
        re.M,
    )
    if not m_wr:
        raise RuntimeError("Could not resolve ZoomManager module WeakReference field")
    syms["zm_mod_field"] = m_wr.group(1)
    syms["module_iface"] = m_wr.group(2)

    # Find ZoomProtocol implemented by ZoomManager that has `a()Ljava/util/Optional;` and `()Landroid/util/Range;`
    for imp in re.findall(r"^\.implements (L[^;\s]+;)", zm_txt, re.M):
        imp_rel = imp.strip("L;") + ".smali"
        imp_path = next((p for p in ws.smali_files if str(p).endswith("/" + imp_rel)), None)
        if not imp_path:
            continue
        itxt = ws.read(imp_path)
        m_opt = re.search(r"^\.method public static (\w+)\(\)Ljava/util/Optional;\s*$", itxt, re.M)
        m_rng = re.search(r"^\.method public abstract (\w+)\(\)Landroid/util/Range;\s*$", itxt, re.M)
        if m_opt and m_rng:
            syms["zoom_proto_cls"] = imp
            syms["zoom_proto_opt_m"] = m_opt.group(1)
            syms["zoom_proto_rng_m"] = m_rng.group(1)
            break
    if "zoom_proto_cls" not in syms:
        raise RuntimeError("Could not resolve ZoomProtocol interface")

    # Module->getCameraManager()Lm6/l; -> Lm6/l;->b()Ln9/f; -> Ln9/f;->B()F (SCALER_AVAILABLE_MAX_DIGITAL_ZOOM)
    mod_rel = syms["module_iface"].strip("L;") + ".smali"
    mod_path = next((p for p in ws.smali_files if str(p).endswith("/" + mod_rel)), None)
    mod_txt = ws.read(mod_path)
    m_cm = re.search(r"^\.method public abstract getCameraManager\(\)(L[^;\s]+;)\s*$", mod_txt, re.M)
    if not m_cm:
        raise RuntimeError("Could not resolve Module.getCameraManager()")
    syms["cam_mgr_iface"] = m_cm.group(1)

    caps_hits = ws.find_files_containing("SCALER_AVAILABLE_MAX_DIGITAL_ZOOM:Landroid/hardware/camera2/CameraCharacteristics$Key;")
    caps_cls = caps_max_m = None
    for cp in caps_hits:
        ctxt = ws.read(cp)
        for mm in re.finditer(r"^\.method public final (\w+)\(\)F\n(.*?)^\.end method", ctxt, re.M | re.S):
            if "SCALER_AVAILABLE_MAX_DIGITAL_ZOOM" in mm.group(2):
                mc_c = re.search(r"^\.class[^\n]+(L[^;\s]+;)", ctxt, re.M)
                if mc_c:
                    caps_cls = mc_c.group(1)
                    caps_max_m = mm.group(1)
                    break
        if caps_cls:
            break
    if not (caps_cls and caps_max_m):
        raise RuntimeError("Could not resolve CameraCapabilities max digital zoom method")
    syms["caps_cls"] = caps_cls
    syms["caps_max_m"] = caps_max_m

    cm_rel = syms["cam_mgr_iface"].strip("L;") + ".smali"
    cm_path = next((p for p in ws.smali_files if str(p).endswith("/" + cm_rel)), None)
    cm_txt = ws.read(cm_path)
    m_getcaps = re.search(rf"^\.method public abstract (\w+)\(\){re.escape(caps_cls)}\s*$", cm_txt, re.M)
    if not m_getcaps:
        raise RuntimeError(f"Could not resolve {syms['cam_mgr_iface']}->{caps_cls} getter")
    syms["cam_mgr_caps_m"] = m_getcaps.group(1)

    # Fragment title string ID for ZoomModSettingsFragment ("Zoom") & ProModSettingsFragment ("Pro")
    syms["zoom_title_res"] = hex(ws.r_string.get("parameter_zoom_slider_title", 0x7F140CDA))
    syms["pro_title_res"] = hex(
        ws.r_string.get("module_name_professional")
        or ws.r_string.get("pref_camera_settings_category")
        or ws.r_string.get("camera_setting")
        or 0x7F140CDA
    )

    # 7. Pro Mod symbols: MediaRecorderParameter (Lsq/o;) & its exact field names from toString()
    mrp_hits = ws.find_files_containing('"MediaRecorderParameter{mEnableAudio="', '", mVideoEncodingBitRate="')
    if not mrp_hits:
        raise RuntimeError("Could not locate MediaRecorderParameter class")
    mrp_txt = ws.read(mrp_hits[0])
    mc_mrp = re.search(r"^\.class[^\n]+(L[^;\s]+;)", mrp_txt, re.M)
    syms["mrp_cls"] = mc_mrp.group(1)

    def _mrp_field(label: str, ftype: str) -> str:
        pat = re.compile(
            re.escape(label) + r'"[\s\S]{0,160}?iget(?:-boolean|-wide)? v\d+, p0, '
            + re.escape(syms["mrp_cls"]) + r"->(\w+):" + re.escape(ftype)
        )
        mm = pat.search(mrp_txt)
        if not mm:
            raise RuntimeError(f"Could not resolve {syms['mrp_cls']} field for {label}")
        return mm.group(1)

    syms["mrp_enable_audio"] = _mrp_field("MediaRecorderParameter{mEnableAudio=", "Z")
    syms["mrp_audio_bitrate"] = _mrp_field(", mAudioEncodingBitRate=", "I")
    syms["mrp_audio_sr"] = _mrp_field(", mAudioSamplingRate=", "I")
    syms["mrp_video_bitrate"] = _mrp_field(", mVideoEncodingBitRate=", "I")
    syms["mrp_max_duration"] = _mrp_field(", mMaxDuration=", "I")
    syms["mrp_max_filesize"] = _mrp_field(", mMaxFileSize=", "J")

    # 8. TaskDataStorageWriter (Lv7/j;) for Ultra HDR lossless stream filtering
    sw_hits = ws.find_files_containing('"TaskDataStorageWriter"', '"writeTo: imageData is null, write nothing"')
    if not sw_hits:
        raise RuntimeError("Could not locate TaskDataStorageWriter class")
    sw_txt = ws.read(sw_hits[0])
    mc_sw = re.search(r"^\.class[^\n]+(L[^;\s]+;)", sw_txt, re.M)
    mm_sw = re.search(r"^\.method public final (\w+)\(Ljava/io/OutputStream;\)V\s*$", sw_txt, re.M)
    if not (mc_sw and mm_sw):
        raise RuntimeError("Could not resolve TaskDataStorageWriter.writeTo(OutputStream)")
    syms["sw_path"] = sw_hits[0]
    syms["sw_cls"] = mc_sw.group(1)
    syms["sw_write_m"] = mm_sw.group(1)
    syms["sw_orig_m"] = f"{mm_sw.group(1)}_onyxOrig"

    ws._port_helper_syms = syms
    return syms


def ensure_onyx_port_helper(ws: SmaliWorkspace) -> Path:
    """
    Compile clean Java port sources (java/com/onyx/port/*.java) via
    javac -> d8 -> baksmali and remap all symbolic R8 placeholders into
    a dedicated overflow smali_classes{N+1}/com/onyx/port/ directory.
    """
    existing = getattr(ws, "_onyx_port_helper_dir", None)
    if existing is not None:
        return existing

    from compile_port_java import compile_port_java_to_smali

    s = _discover_port_helper_symbols(ws)

    # Reuse existing com/onyx/port dir or create an isolated overflow smali_classes{max+1}
    port_dir = None
    for d in sorted(ws.root.glob("smali_classes*")):
        if (d / "com" / "onyx" / "port" / "OnyxPortHelper.smali").exists():
            port_dir = d / "com" / "onyx" / "port"
            break
    if port_dir is None:
        nums = [1]
        for d in ws.root.glob("smali_classes*"):
            m = re.match(r"smali_classes(\d+)$", d.name)
            if m:
                nums.append(int(m.group(1)))
        port_dir = ws.root / f"smali_classes{max(nums) + 1}" / "com" / "onyx" / "port"

    port_root = Path(__file__).resolve().parent.parent
    written = compile_port_java_to_smali(port_root, s, port_dir)
    for wf in written:
        ws.modified_paths.add(wf)
    ws._onyx_port_helper_dir = port_dir
    return port_dir


# ---------------------------------------------------------------------------
# Reusable Smali Hook Primitives (avoid duplicating raw bytecode templates)
# ---------------------------------------------------------------------------

def _hook_void_guard(ws: SmaliWorkspace, path: Path, method_re: str, helper_pred: str, label: str = ":cond_onyx_g") -> bool:
    """Inject `if (OnyxPortHelper.<helper_pred>()) return;` at the entry of a matching void method."""
    txt = ws.read(path)
    if f"OnyxPortHelper;->{helper_pred}" in txt:
        return True
    m = re.search(method_re, txt, re.M)
    if not m:
        return False
    guard = (
        f"\n    invoke-static {{}}, Lcom/onyx/port/OnyxPortHelper;->{helper_pred}()Z\n\n"
        f"    move-result v0\n\n"
        f"    if-eqz v0, {label}\n\n"
        f"    return-void\n\n"
        f"    {label}\n"
    )
    ws.write(path, txt[: m.end()] + guard + txt[m.end() :])
    return True


def _hook_bool_guard(ws: SmaliWorkspace, path: Path, method_re: str, helper_pred: str, ret_val: int = 0, label: str = ":cond_onyx_gb") -> bool:
    """Inject `if (OnyxPortHelper.<helper_pred>()) return <ret_val>;` (bumping .locals>=1) at method entry."""
    txt = ws.read(path)
    if f"OnyxPortHelper;->{helper_pred}" in txt:
        return True
    m = re.search(method_re, txt, re.M)
    if not m:
        return False
    new_loc = max(1, int(m.group(2)))
    guard = (
        f"\n    invoke-static {{}}, Lcom/onyx/port/OnyxPortHelper;->{helper_pred}()Z\n\n"
        f"    move-result v0\n\n"
        f"    if-eqz v0, {label}\n\n"
        f"    const/4 v0, {hex(ret_val)}\n\n"
        f"    return v0\n\n"
        f"    {label}\n"
    )
    ws.write(path, txt[: m.start()] + f"{m.group(1)}{new_loc}{m.group(3)}{guard}" + txt[m.end() :])
    return True


def patch_compat_fix_50mp_blob_buffer(ws: SmaliWorkspace):
    """
    Fix Android 17 50MP half-photo truncation (4.48MB clamp in SessionConfigurationUtils):
    Route MockCameraImageReceiver.createCaptureSession()'s ImageReader.newInstance(w,h,fmt,max)
    through OnyxPortHelper.createImageReader() which passes usage=0x100003L
    (USAGE_CPU_READ_OFTEN | GRALLOC_USAGE_RENDERSCRIPT) for high-res JPEG/HEIF streams,
    setting flexibleConsumer=false and bypassing roundBufferDimensionNearest().
    """
    ensure_onyx_port_helper(ws)
    hits = ws.find_files_containing('"createCaptureSession: imageFormat: "', "MockCameraImageReceiver")
    if not hits:
        return False, "MockCameraImageReceiver not found"
    p = hits[0]
    txt = ws.read(p)
    target = "Landroid/media/ImageReader;->newInstance(IIII)Landroid/media/ImageReader;"
    repl = "Lcom/onyx/port/OnyxPortHelper;->createImageReader(IIII)Landroid/media/ImageReader;"
    if repl in txt:
        return True, f"Already hooked 50MP ImageReader in {p.relative_to(ws.root)}"
    if target not in txt:
        return False, f"ImageReader.newInstance(IIII) not found in {p.relative_to(ws.root)}"
    ws.write(p, txt.replace(target, repl, 1))
    return True, f"Hooked 50MP ImageReader buffer allocation in {p.relative_to(ws.root)}"


def patch_compat_fix_ultrahdr_when_hdr_off(ws: SmaliWorkspace):
    """
    Fix onyx HAL Ultra HDR GainMap leak when HDR is turned OFF:
    Onyx's DeviceSessionPolicy::isUltraHdrEnabled never checks xiaomi.hdr.hdrMode, so MIVI
    always embeds an Ultra HDR GainMap even when EXIF records "Hdr":"off".
    Losslessly strips GainMap (XMP hdrgm, APP2 MPF, secondary GainMap JPEG) at save time
    without re-encoding across all ByteBuffer and TaskDataStorageWriter save paths.
    """
    ensure_onyx_port_helper(ws)
    s = _discover_port_helper_symbols(ws)
    notes = []

    # 1. Hook Storage (n7/M) ByteBuffer save methods: addImage (b), writeFile (A), updateImage (J)
    st_hits = ws.find_files_containing('"addImage: parallel="', '"Failed to write image"')
    if not st_hits:
        return False, "Storage class (addImage / Failed to write image) not found"
    st_p = st_hits[0]
    st_txt = ws.read(st_p)
    st_hooks = 0

    buf_methods = [
        # addImage(..., ByteBuffer p6, ...)
        (
            r"(\.method public static \w+\(Landroid/app/Application;Ljava/lang/String;JLandroid/location/Location;ILjava/nio/ByteBuffer;ZIIZZJIZIIII\)Landroid/net/Uri;\n\s+\.locals \d+\n)",
            "p6",
            "addImage",
        ),
        # writeFile(Context, ByteBuffer p1, String, Uri, int)Z
        (
            r"(\.method public static \w+\(Landroid/content/Context;Ljava/nio/ByteBuffer;Ljava/lang/String;Landroid/net/Uri;I\)Z\n\s+\.locals \d+(?:\n\s+\.annotation[\s\S]*?\.end annotation)?\n)",
            "p1",
            "writeFile",
        ),
        # updateImageWithByteBuffer(Application, Uri, String, ByteBuffer p3, ...)Z
        (
            r"(\.method public static \w+\(Landroid/app/Application;Landroid/net/Uri;Ljava/lang/String;Ljava/nio/ByteBuffer;Ljava/lang/String;ZILjava/io/File;Ljava/lang/String;\)Z\n\s+\.locals \d+\n)",
            "p3",
            "updateImage",
        ),
    ]
    for pat, reg, label in buf_methods:
        m = re.search(pat, st_txt, re.M)
        if not m:
            continue
        if "OnyxPortHelper;->filterUltraHdrBuffer" in st_txt[m.start() : m.end() + 260]:
            st_hooks += 1
            continue
        hook = (
            f"\n    invoke-static/range {{{reg} .. {reg}}}, Lcom/onyx/port/OnyxPortHelper;->filterUltraHdrBuffer(Ljava/nio/ByteBuffer;)Ljava/nio/ByteBuffer;\n\n"
            f"    move-result-object {reg}\n"
        )
        st_txt = st_txt[: m.end()] + hook + st_txt[m.end() :]
        st_hooks += 1
        notes.append(f"{st_p.stem}.{label}")
    if st_hooks == 0:
        return False, f"Could not hook any ByteBuffer save methods in {st_p.name}"
    ws.write(st_p, st_txt)

    # 2. Hook TaskDataStorageWriter (v7/j) streaming writer: rename b -> b_onyxOrig + trampoline to writeFilteredStream
    sw_p = s["sw_path"]
    sw_txt = ws.read(sw_p)
    sw_cls = s["sw_cls"]
    sw_m = s["sw_write_m"]
    sw_orig = s["sw_orig_m"]
    if "OnyxPortHelper;->writeFilteredStream" not in sw_txt:
        decl = f".method public final {sw_m}(Ljava/io/OutputStream;)V"
        if decl not in sw_txt:
            return False, f"{decl} not found in {sw_p.name}"
        trampoline = (
            f".method public final {sw_m}(Ljava/io/OutputStream;)V\n"
            f"    .locals 0\n\n"
            f"    invoke-static {{p0, p1}}, Lcom/onyx/port/OnyxPortHelper;->writeFilteredStream({sw_cls}Ljava/io/OutputStream;)V\n\n"
            f"    return-void\n"
            f".end method\n\n"
            f".method public final {sw_orig}(Ljava/io/OutputStream;)V"
        )
        sw_txt = sw_txt.replace(decl, trampoline, 1)
        ws.write(sw_p, sw_txt)
    notes.append(f"{sw_p.stem}.{sw_m}")

    return True, "Installed lossless Ultra HDR GainMap filter: " + ", ".join(notes)


def patch_smooth_zoom_menu_and_default(ws: SmaliWorkspace):
    """
    Full Smooth Zoom (Mod) port + improvements (default ON):
    1. Inject 'Smooth Zoom (Mod)' menu in OtherSettingFragments, CameraCommonPreferenceFragment,
       CameraCapturePreferenceFragment, and CameraCamcorderPreferenceFragment -> opens full
       ZoomModSettingsFragment (7 tuned Bezier curves, 6 speeds, 10x extra buttons, keep buttons
       while recording, full 10x+ video zoom range — all ON by default).
    2. Hook all 7 zoom engine sites dynamically:
       - I4/W (Zoom toggle ValueAnimator): forcePerFrame(Z) + applyDuration(ValueAnimator) + et() gate
       - Vr/i.n(FF): applyDuration(ValueAnimator) for smooth curve transitions
       - I4/Y: collapseInRecord(Z) to keep zoom buttons visible while recording
       - data/j: extendRatios([F)[F to inject 3x/5x/7.5x/10x extra zoom buttons
       - data/o: filterTargetZoom(F)F to eliminate abrupt lens-switch zoom snaps
       - k9/l.T8: widenZoomRange() to lift video zoom range up to full sensor max (10x+)
    """
    ensure_onyx_port_helper(ws)
    s = _discover_port_helper_symbols(ws)
    notes = []

    # 1. Inject single clean 'Smooth Zoom (Mod)' entry in OtherSettingFragments & CameraCommonPreferenceFragment
    #    IMPORTANT: inject at the START of addCheckUpgradePreference (before stock code overwrites p0!)
    #    so p0 is guaranteed to be `this` (com.android.camera.fragment.settings.b) for ZoomModClickListener!
    for cls_name in ("OtherSettingFragments.smali", "CameraCommonPreferenceFragment.smali"):
        hits = [p for p in ws.smali_files if p.name == cls_name]
        if not hits:
            continue
        p = hits[0]
        txt = ws.read(p)
        if "OnyxPortHelper;->addZoomPreference" in txt:
            notes.append(f"{cls_name}(already)")
            continue
        mpat = re.compile(
            r"(\.method private addCheckUpgradePreference\(Landroidx/preference/PreferenceCategory;\)V\n\s+\.locals \d+\n)",
            re.M,
        )
        m = mpat.search(txt)
        if m:
            inject = "\n    invoke-static {p0, p1}, Lcom/onyx/port/OnyxPortHelper;->addZoomPreference(Lcom/android/camera/fragment/settings/b;Landroidx/preference/PreferenceCategory;)V\n"
            ws.write(p, txt[: m.end()] + inject + txt[m.end() :])
            notes.append(cls_name)

    # 2. Refresh 'Smooth Zoom (Mod)' summary live on CameraPreferenceFragment.onResume() when returning from subpage
    cpf_p = next((p for p in ws.smali_files if p.name == "CameraPreferenceFragment.smali"), None)
    if cpf_p:
        cpf_t = ws.read(cpf_p)
        if "OnyxPortHelper;->refreshZoomModSummary" not in cpf_t:
            rmpat = re.compile(
                r"(\.method public onResume\(\)V\n\s+\.locals \d+[\s\S]{0,400}?)(    return-void\n\.end method)",
                re.M,
            )
            rm = rmpat.search(cpf_t)
            if rm:
                rinject = (
                    "    iget-object v0, p0, Lcom/android/camera/fragment/settings/b;->mPreferenceGroup:Landroidx/preference/PreferenceScreen;\n\n"
                    "    invoke-static {v0}, Lcom/onyx/port/OnyxPortHelper;->refreshZoomModSummary(Landroidx/preference/PreferenceGroup;)V\n\n"
                )
                ws.write(cpf_p, cpf_t[: rm.start()] + rm.group(1) + rinject + rm.group(2) + cpf_t[rm.end() :])
                notes.append("CameraPreferenceFragment.onResume")

    # 3. Universal click handler in CameraPreferenceFragment & OtherSettingFragments -> opens ZoomModSettingsFragment
    for clk_cls in ("CameraPreferenceFragment.smali", "OtherSettingFragments.smali"):
        cpf = next((p for p in ws.smali_files if p.name == clk_cls), None)
        if not cpf:
            return False, f"{clk_cls} not found"
        ct = ws.read(cpf)
        if "OnyxPortHelper;->handleZoomModClick" not in ct:
            mpat = re.compile(
                r"(\.method public onPreferenceClick\(Landroidx/preference/Preference;\)Z\n\s+\.locals \d+[\s\S]{0,120}?iget-object p1, p1, Landroidx/preference/Preference;->\w+:Ljava/lang/String;\s+)",
                re.M,
            )
            m = mpat.search(ct)
            if not m:
                return False, f"onPreferenceClick not found in {clk_cls}"
            # Use v0/v3 safely — in OtherSettingFragments v0=6, v1=0, v2=1 are set before iget-object, so use v3 if locals>=4 else v0!
            m_loc = re.search(r"\.locals (\d+)", m.group(1))
            treg = "v3" if (m_loc and int(m_loc.group(1)) >= 4) else "v0"
            hook = (
                f"invoke-static {{p0, p1}}, Lcom/onyx/port/OnyxPortHelper;->handleZoomModClick(Lcom/android/camera/fragment/settings/b;Ljava/lang/String;)Z\n\n"
                f"    move-result {treg}\n\n"
                f"    if-eqz {treg}, :cond_onyx_zm_clk\n\n"
                f"    const/4 p0, 0x1\n\n"
                f"    return p0\n\n"
                f"    :cond_onyx_zm_clk\n    "
            )
            ws.write(cpf, ct[: m.end()] + hook + ct[m.end() :])
            notes.append(f"{cpf.stem}.click")

    # 4. Hook I4/W (Zoom toggle animator): forcePerFrame + applyDuration + et() gate
    czs_hits = ws.find_files_containing('"changeZoomRatioSmoothly: mZoomRatioToggleProcessAnimator"')
    if not czs_hits:
        return False, "Zoom toggle fragment (I4/W) not found"
    i4w_p = czs_hits[0]
    i4w_txt = ws.read(i4w_p)
    mc_w = re.search(r"^\.class[^\n]+(L[^;\s]+;)", i4w_txt, re.M).group(1)
    i4w_changed = False

    # 4a. et() gate
    idx = i4w_txt.find('"changeZoomRatioSmoothly: mZoomRatioToggleProcessAnimator"')
    pre = i4w_txt[max(0, idx - 1200) : idx]
    m_gate = re.search(rf"invoke-static \{{\}}, {re.escape(mc_w)}->(\w+)\(\)Z", pre)
    if m_gate:
        gate_m = m_gate.group(1)
        gm_pat = re.compile(rf"(\.method public static {re.escape(gate_m)}\(\)Z\n\s+\.locals \d+\n)", re.M)
        mg = gm_pat.search(i4w_txt)
        if mg and "OnyxPortHelper;->isSmoothZoomEnabled" not in i4w_txt[mg.start() : mg.end() + 220]:
            hook = (
                "\n    invoke-static {}, Lcom/onyx/port/OnyxPortHelper;->isSmoothZoomEnabled()Z\n\n"
                "    move-result v0\n\n"
                "    if-nez v0, :cond_onyx_sz\n\n"
                "    const/4 v0, 0x0\n\n"
                "    return v0\n\n"
                "    :cond_onyx_sz\n"
            )
            i4w_txt = i4w_txt[: mg.end()] + hook + i4w_txt[mg.end() :]
            i4w_changed = True
            notes.append(f"{mc_w}->{gate_m}()")

    # 4b. forcePerFrame(Z) right before new-instance vX, LI4/W$b; invoke-direct {vX, p0, p2, vFlag, p1}, LI4/W$b;-><init>(LI4/W;FZZ)V
    if "OnyxPortHelper;->forcePerFrame" not in i4w_txt:
        pat_fpf = re.compile(
            rf"(new-instance (v\d+), {re.escape(mc_w[:-1])}\$\w+;\s+invoke-direct \{{\2, p0, p2, (v\d+), p1\}}, {re.escape(mc_w[:-1])}\$\w+;-><init>\({re.escape(mc_w)}FZZ\)V)"
        )
        mf = pat_fpf.search(i4w_txt)
        if not mf:
            return False, f"Could not locate forcePerFrame site in {i4w_p.name}"
        freg = mf.group(3)
        inject_fpf = (
            f"invoke-static {{{freg}}}, Lcom/onyx/port/OnyxPortHelper;->forcePerFrame(Z)Z\n\n"
            f"    move-result {freg}\n\n"
            f"    {mf.group(1)}"
        )
        i4w_txt = i4w_txt[: mf.start()] + inject_fpf + i4w_txt[mf.end() :]
        i4w_changed = True
        notes.append(f"{mc_w}.forcePerFrame")

    # 4c. applyDuration(ValueAnimator) right before m.start() in st(FF)
    if "OnyxPortHelper;->applyDuration" not in i4w_txt:
        pat_ad = re.compile(
            rf"(invoke-static \{{([pv]\d+)\}}, L[^;]+;->\w+\(Landroid/animation/ValueAnimator;\)V)(\s+iget-object p0, p0, {re.escape(mc_w)}->\w+:Landroid/animation/ValueAnimator;\s+invoke-virtual \{{p0\}}, Landroid/animation/ValueAnimator;->start\(\)V)"
        )
        ma = pat_ad.search(i4w_txt)
        if not ma:
            return False, f"Could not locate applyDuration site in {i4w_p.name}"
        areg = ma.group(2)
        inject_ad = (
            f"{ma.group(1)}\n\n"
            f"    invoke-static {{{areg}}}, Lcom/onyx/port/OnyxPortHelper;->applyDuration(Landroid/animation/ValueAnimator;)V"
            f"{ma.group(3)}"
        )
        i4w_txt = i4w_txt[: ma.start()] + inject_ad + i4w_txt[ma.end() :]
        i4w_changed = True
        notes.append(f"{mc_w}.applyDuration")

    if i4w_changed:
        ws.write(i4w_p, i4w_txt)

    # 5. Hook Vr/i.n(FF)Landroid/animation/ValueAnimator; -> applyDuration(v0)
    vri_hits = ws.find_files_containing("Landroid/animation/ValueAnimator;->setFloatValues([F)V", "Landroid/animation/ValueAnimator;->setDuration(J)Landroid/animation/ValueAnimator;")
    for vp in vri_hits:
        vtxt = ws.read(vp)
        if "OnyxPortHelper;->applyDuration" in vtxt:
            notes.append(f"{vp.name}(already)")
            break
        pat_vr = re.compile(
            r"(\.method public static \w+\(FF\)Landroid/animation/ValueAnimator;[\s\S]{0,1200}?invoke-virtual \{(v\d+), [pv]\d+, [pv]\d+\}, Landroid/animation/ValueAnimator;->setDuration\(J\)Landroid/animation/ValueAnimator;\s+)(return-object \2\n\.end method)"
        )
        mv = pat_vr.search(vtxt)
        if mv:
            vreg = mv.group(2)
            inject_vr = f"{mv.group(1)}invoke-static {{{vreg}}}, Lcom/onyx/port/OnyxPortHelper;->applyDuration(Landroid/animation/ValueAnimator;)V\n\n    {mv.group(3)}"
            ws.write(vp, vtxt[: mv.start()] + inject_vr + vtxt[mv.end() :])
            notes.append(f"{vp.stem}.applyDuration")
            break

    # 6. Hook I4/Y -> collapseInRecord(Z) right before `iput-boolean vX, p1, ...;->d:Z ; return-void`
    i4y_hits = ws.find_files_containing('"isMotionSupportZoomPanel"')
    if i4y_hits:
        yp = i4y_hits[0]
        ytxt = ws.read(yp)
        if "OnyxPortHelper;->collapseInRecord" not in ytxt:
            pat_cir = re.compile(
                r"(iput-boolean v\d+, p1, (L[^;]+;)->b:Z\s+:\w+\s+)(iput-boolean v\d+, p1, \2->d:Z\s+return-void)"
            )
            my = pat_cir.search(ytxt)
            if my:
                holder = my.group(2)
                inject_cir = (
                    f"{my.group(1)}"
                    f"iget-boolean p0, p1, {holder}->b:Z\n\n"
                    f"    invoke-static {{p0}}, Lcom/onyx/port/OnyxPortHelper;->collapseInRecord(Z)Z\n\n"
                    f"    move-result p0\n\n"
                    f"    iput-boolean p0, p1, {holder}->b:Z\n\n"
                    f"    {my.group(3)}"
                )
                ws.write(yp, ytxt[: my.start()] + inject_cir + ytxt[my.end() :])
                notes.append(f"{yp.stem}.collapseInRecord")

    # 7. Hook com/android/camera/data/data/j.smali -> extendRatios([F)[F
    dj_p = next((p for p in ws.smali_files if str(p).endswith("/com/android/camera/data/data/j.smali")), None)
    if dj_p:
        dj_txt = ws.read(dj_p)
        if "OnyxPortHelper;->extendRatios" not in dj_txt:
            pat_er = re.compile(
                r"(new-array p0, p0, \[F[\s\S]{0,260}?aput v\d+, p0, v\d+[\s\S]{0,80}?:\w+\s+)(return-object p0\s+nop\s+:pswitch_data_0\s+\.packed-switch 0xa1)"
            )
            me = pat_er.search(dj_txt)
            if not me:
                return False, "Could not locate extendRatios site in data/j.smali"
            inject_er = (
                f"{me.group(1)}"
                "invoke-static {p0}, Lcom/onyx/port/OnyxPortHelper;->extendRatios([F)[F\n\n"
                "    move-result-object p0\n\n"
                f"    {me.group(2)}"
            )
            ws.write(dj_p, dj_txt[: me.start()] + inject_er + dj_txt[me.end() :])
            notes.append("data/j.extendRatios")

    # 8. Hook com/android/camera/data/data/o.smali (pref_camera_target_zoom_key setter) -> filterTargetZoom(F)F
    tz_hits = ws.find_files_containing('"pref_camera_target_zoom_key"')
    for tp in tz_hits:
        ttxt = ws.read(tp)
        if "OnyxPortHelper;->filterTargetZoom" in ttxt:
            break
        pat_tz = re.compile(
            r'(\.method public static \w+\(F\)V\n\s+\.locals \d+\n)([\s\S]{0,260}?"pref_camera_target_zoom_key")'
        )
        mt = pat_tz.search(ttxt)
        if mt:
            inject_tz = (
                "\n    invoke-static {p0}, Lcom/onyx/port/OnyxPortHelper;->filterTargetZoom(F)F\n\n"
                "    move-result p0\n"
            )
            ws.write(tp, ttxt[: mt.start()] + mt.group(1) + inject_tz + mt.group(2) + ttxt[mt.end() :])
            notes.append(f"{tp.stem}.filterTargetZoom")
            break

    # 9. Hook ZoomManager (k9/l.T8) -> widenZoomRange()
    zm_p = s["zm_path"]
    zm_txt = ws.read(zm_p)
    if "OnyxPortHelper;->widenZoomRange" not in zm_txt:
        pat_wz = re.compile(
            r'(\.method public \w+\(Landroid/util/Range;\)V\n[\s\S]{0,260}?)(\s+const-string(?:/jumbo)? v\d+, "setZoomRangeWithUI\(\): zoomRange = ")'
        )
        mw = pat_wz.search(zm_txt)
        if not mw:
            return False, f"Could not locate widenZoomRange site in {zm_p.name}"
        inject_wz = (
            f"{mw.group(1)}\n"
            f"    invoke-static {{p0, p1}}, Lcom/onyx/port/OnyxPortHelper;->widenZoomRange({s['zm_cls']}Landroid/util/Range;)Landroid/util/Range;\n\n"
            f"    move-result-object p1\n"
            f"{mw.group(2)}"
        )
        ws.write(zm_p, zm_txt[: mw.start()] + inject_wz + zm_txt[mw.end() :])
        notes.append(f"{zm_p.stem}.widenZoomRange")

    return True, "Ported & upgraded full Smooth Zoom (Mod) [default ON, 7 curves, 10x+]: " + ", ".join(notes)


def patch_camera_pro_mod_menu(ws: SmaliWorkspace):
    """
    Camera Quality & Pro Mod settings page + 6 dynamic engine hooks:
      1. Video bitrate override (Stock / 35..200 Mbps / Custom Mbps)
      2. Audio bitrate override (Stock / 192..512 kbps AAC @ 48 kHz)
      3. Photo JPEG/HEIC quality % (Custom numeric 1-100%, 0=Stock; 50MP auto-capped <=95%)
      4. Unlimited video recording duration & file size (default ON)
      5. Bypass thermal & Slow-Motion overheat limit (default ON)
      6. Bypass low-battery flash & video restrictions (default ON)
      7. Force mute shutter & video start/stop sounds (default OFF)
    """
    ensure_onyx_port_helper(ws)
    s = _discover_port_helper_symbols(ws)
    notes = []

    # 1. Inject 'Camera Quality & Pro Mod' row in OtherSettingFragments & CameraCommonPreferenceFragment
    for cls_name in ("OtherSettingFragments.smali", "CameraCommonPreferenceFragment.smali"):
        hits = [p for p in ws.smali_files if p.name == cls_name]
        if not hits:
            continue
        p = hits[0]
        txt = ws.read(p)
        if "OnyxPortHelper;->addProModPreference" in txt:
            notes.append(f"{cls_name}(already)")
            continue
        zoom_call = "invoke-static {p0, p1}, Lcom/onyx/port/OnyxPortHelper;->addZoomPreference(Lcom/android/camera/fragment/settings/b;Landroidx/preference/PreferenceCategory;)V"
        pro_call = "invoke-static {p0, p1}, Lcom/onyx/port/OnyxPortHelper;->addProModPreference(Lcom/android/camera/fragment/settings/b;Landroidx/preference/PreferenceCategory;)V"
        if zoom_call in txt:
            ws.write(p, txt.replace(zoom_call, zoom_call + "\n\n    " + pro_call, 1))
            notes.append(cls_name)
        else:
            mpat = re.compile(
                r"(\.method private addCheckUpgradePreference\(Landroidx/preference/PreferenceCategory;\)V\n\s+\.locals \d+\n)",
                re.M,
            )
            m = mpat.search(txt)
            if m:
                ws.write(p, txt[: m.end()] + "\n    " + pro_call + "\n" + txt[m.end() :])
                notes.append(cls_name)

    # 2. Hook all MediaRecorder / MediaCodecRecorder / CinematicRecorder parameter setup methods
    mrp_cls = s["mrp_cls"]
    rec_hooks = 0
    for tag in ('"CED_MiMediaRecorder"', '"CED_MiMediaCodecRecorder"', '"CED_MiCinematicRecorder"'):
        for rp in ws.find_files_containing(tag, f"({mrp_cls})V"):
            rtxt = ws.read(rp)
            if "OnyxPortHelper;->applyVideoParams" in rtxt:
                rec_hooks += 1
                continue
            mpat = re.compile(
                rf"(\.method public final \w+\({re.escape(mrp_cls)}\)V\n\s+\.locals \d+\n)",
                re.M,
            )
            m = mpat.search(rtxt)
            if m:
                hook = f"\n    invoke-static {{p1}}, Lcom/onyx/port/OnyxPortHelper;->applyVideoParams({mrp_cls})V\n"
                ws.write(rp, rtxt[: m.end()] + hook + rtxt[m.end() :])
                rec_hooks += 1
    if rec_hooks == 0:
        return False, "Could not hook any video recorder setMediaRecorderParameter()"
    notes.append(f"videoRecorders({rec_hooks})")

    # 3. Hook PhotoBase.getPhotoQuality(Z)I -> OnyxPortHelper.applyPhotoQuality(moduleIndex, quality)
    pb_hits = [p for p in ws.smali_files if p.name == "PhotoBase.smali"]
    if not pb_hits:
        return False, "PhotoBase.smali not found"
    pb_p = pb_hits[0]
    pb_txt = ws.read(pb_p)
    if "OnyxPortHelper;->applyPhotoQuality" not in pb_txt:
        mpat = re.compile(
            r"(\.method public final getPhotoQuality\(Z\)I\n)(\s+\.locals )(\d+)([\s\S]{0,1200}?)(    return p0\n\.end method)",
            re.M,
        )
        m = mpat.search(pb_txt)
        if not m:
            return False, "PhotoBase.getPhotoQuality(Z)I not found"
        orig_loc = int(m.group(3))
        save_reg = f"v{orig_loc}"
        entry_save = (
            f"\n\n    invoke-virtual {{p0}}, Lcom/xiaomi/camera/module/PhotoBase;->getModuleIndex()I\n\n"
            f"    move-result {save_reg}\n"
        )
        ret_hook = (
            f"    invoke-static {{{save_reg}, p0}}, Lcom/onyx/port/OnyxPortHelper;->applyPhotoQuality(II)I\n\n"
            f"    move-result p0\n\n"
        )
        new_method = m.group(1) + m.group(2) + str(orig_loc + 1) + entry_save + m.group(4) + ret_hook + m.group(5)
        ws.write(pb_p, pb_txt[: m.start()] + new_method + pb_txt[m.end() :])
        notes.append("PhotoBase.getPhotoQuality")

    # 4. Thermal & Slow-Motion Overheat Limit Bypass (5 surgical sites)
    th_sites = [
        (('"action_temp_state_change"', '"onReceive stage = "'),
         r"(\.method public final onReceive\(Landroid/content/Context;Landroid/content/Intent;\)V\n\s+\.locals \d+\n)"),
        (('"onThermalNotification stage="', '"ThermalDetector"'),
         r"(\.method public final \w+\(I\)V\n\s+\.locals \d+\n)"),
        (('"CameraThermalResult"', "acceptResult()V"),
         r"(\.method public final acceptResult\(\)V\n\s+\.locals \d+\n)"),
        (('"CameraThermalLevelSimpleASD"', "acceptResult()V"),
         r"(\.method public final acceptResult\(\)V\n\s+\.locals \d+\n)"),
    ]
    th_count = sum(
        1 for needles, pat in th_sites for tp in ws.find_files_containing(*needles)
        if _hook_void_guard(ws, tp, pat, "isBypassThermal", ":cond_onyx_th")
    )
    for sm_p in [p for p in ws.smali_files if p.name == "SlowMotionModule.smali"][:1]:
        if _hook_void_guard(
            ws, sm_p,
            r"(\.method public showHighTemperatureTips\(\)V\n\s+\.locals \d+(?:\n\s+\.annotation[\s\S]*?\.end annotation)?\n)",
            "isBypassThermal", ":cond_onyx_th",
        ):
            th_count += 1
    if th_count < 4:
        return False, f"Thermal bypass only hooked {th_count}/5 sites"
    notes.append(f"thermalBypass({th_count})")

    # 5. Low-Battery Flash & Video Bypass (3 surgical sites)
    bat_count = 0
    for bp in ws.find_files_containing('"BatteryDetector"', '"android.intent.action.BATTERY_CHANGED"'):
        if _hook_void_guard(
            ws, bp,
            r"(\.method public final onReceive\(Landroid/content/Context;Landroid/content/Intent;\)V\n\s+\.locals \d+\n)",
            "isBypassLowBattery", ":cond_onyx_bat",
        ):
            bat_count += 1
    for bp in ws.find_files_containing('"camera.debug.lowPower"', '"BatteryDetector"'):
        if _hook_bool_guard(ws, bp, r"(\.method public final \w+\(I\)Z\n\s+\.locals )(\d+)(\n)", "isBypassLowBattery", 0, ":cond_onyx_bat"):
            bat_count += 1
    for vb_p in [p for p in ws.smali_files if p.name == "VideoBase.smali"][:1]:
        if _hook_void_guard(ws, vb_p, r"(\.method public showRecordDialogOnLowBattery\(\)V\n\s+\.locals \d+\n)", "isBypassLowBattery", ":cond_onyx_bat"):
            bat_count += 1
    if bat_count < 2:
        return False, f"Low-battery bypass only hooked {bat_count}/3 sites"
    notes.append(f"batteryBypass({bat_count})")

    # 6. Force Mute Shutter & Video Sounds (MiuiCameraSound play entry)
    snd_ok = False
    for sp in ws.find_files_containing('"MiuiCameraSound"', '"playAudioData fail: "'):
        stxt = ws.read(sp)
        if "OnyxPortHelper;->isForceMuteSound" in stxt:
            snd_ok = True
            break
        for mm in re.finditer(r"(\.method public final (\w+)\(FI\)V\n\s+\.locals \d+\n)([\s\S]*?)(\.end method)", stxt, re.M):
            if '"playAudioData fail: "' in mm.group(3):
                if _hook_void_guard(ws, sp, rf"(\.method public final {re.escape(mm.group(2))}\(FI\)V\n\s+\.locals \d+\n)", "isForceMuteSound", ":cond_onyx_snd"):
                    snd_ok = True
                    notes.append(f"{sp.stem}.forceMute")
                break
        if snd_ok:
            break
    if not snd_ok:
        return False, "Could not locate MiuiCameraSound play method"

    return True, "Installed Camera Quality & Pro Mod (bitrate, audio, custom JPEG %, unlimited dur, thermal/Slow-Mo & battery bypass, mute): " + ", ".join(notes)


def patch_watermark_brand_switcher_menu(ws: SmaliWorkspace):
    """
    Add 'Watermark style' menu inside Frames and watermarks (WmGalleryFragment) to switch between:
      0 = POCO F7 (MIUI Camera)
      1 = Xiaomi 17 Ultra by Leica
      2 = LEITZPHONE powered by Xiaomi
    Hooks Wrapper brand array (Te/b.x), WmGalleryFragment UI, and WatermarkItem MIVI logo sync (a.m0).
    """
    ensure_onyx_port_helper(ws)
    s = _discover_port_helper_symbols(ws)
    notes = []

    # 1. Hook Wrapper->x()[Ljava/lang/String; at method entry
    wtxt = ws.read(ws.wrapper_path)
    if "OnyxPortHelper;->getOverrideBrandArray" not in wtxt:
        mpat = re.compile(rf"(\.method public final {re.escape(s['wrapper_array_m'])}\(\)\[Ljava/lang/String;\n\s+\.locals \d+\n)", re.M)
        m = mpat.search(wtxt)
        if not m:
            return False, f"{s['wrapper_array_m']}()[Ljava/lang/String; not found in {ws.wrapper_path.name}"
        hook = (
            "\n    invoke-static {}, Lcom/onyx/port/OnyxPortHelper;->getOverrideBrandArray()[Ljava/lang/String;\n\n"
            "    move-result-object v0\n\n"
            "    if-eqz v0, :cond_onyx_wm_stock\n\n"
            "    return-object v0\n\n"
            "    :cond_onyx_wm_stock\n"
        )
        ws.write(ws.wrapper_path, wtxt[: m.end()] + hook + wtxt[m.end() :])
        notes.append(f"{ws.wrapper_cls}->{s['wrapper_array_m']}()")

    # 2. Make WmGalleryPreference brand/device fields non-final so live switching updates them cleanly
    wgp = next(p for p in ws.smali_files if p.name == "WmGalleryPreference.smali")
    wgp_txt = ws.read(wgp)
    changed_wgp = False
    for fld in (s["wgp_brand_field"], s["wgp_device_field"]):
        old_decl = f".field public final {fld}:Ljava/lang/String;"
        new_decl = f".field public {fld}:Ljava/lang/String;"
        if old_decl in wgp_txt:
            wgp_txt = wgp_txt.replace(old_decl, new_decl, 1)
            changed_wgp = True
    if changed_wgp:
        ws.write(wgp, wgp_txt)
        notes.append(f"WmGalleryPreference.{s['wgp_brand_field']}/{s['wgp_device_field']} mutable")

    # 3. Make WmGalleryFragment.mWmManager & mWatermarkType public + inject Watermark style pref right below Photo watermark toggle
    wgf = next(p for p in ws.smali_files if p.name == "WmGalleryFragment.smali")
    wgf_txt = ws.read(wgf)
    wgf_txt = wgf_txt.replace(f".field private mWmManager:{s['wm_mgr_cls']}", f".field public mWmManager:{s['wm_mgr_cls']}")
    wgf_txt = wgf_txt.replace(
        ".field private mWatermarkType:Lcom/android/camera/fragment/watermark/wmSettingV2/WmGalleryPreference;",
        ".field public mWatermarkType:Lcom/android/camera/fragment/watermark/wmSettingV2/WmGalleryPreference;",
    )
    if "OnyxPortHelper;->addWatermarkStylePreference" not in wgf_txt:
        pat_add = re.compile(
            r"(invoke-virtual/range \{(v\d+) \.\. v\d+\}, Lcom/android/camera/fragment/settings/b;->addCheckBoxPreference\(Landroidx/preference/PreferenceGroup;Ljava/lang/String;ZII\)Landroidx/preference/CheckBoxPreference;\s+"
            r"move-result-object p0\s+const/4 v\d+, 0x0\s+invoke-virtual \{p0, v\d+\}, Landroidx/preference/Preference;->\w+\(Z\)V)"
        )
        m = pat_add.search(wgf_txt)
        if not m:
            return False, "Could not locate Photo watermark CheckBoxPreference injection point in WmGalleryFragment"
        frag_reg = m.group(2)
        # v4 holds mWatermarkSwitchCategory right before invoke-virtual/range {v3 .. v8}
        cat_reg = f"v{int(frag_reg[1:]) + 1}"
        inject = (
            f"{m.group(1)}\n\n"
            f"    invoke-static {{{frag_reg}, {cat_reg}}}, Lcom/onyx/port/OnyxPortHelper;->addWatermarkStylePreference(Lcom/android/camera/fragment/watermark/wmSettingV2/WmGalleryFragment;Landroidx/preference/PreferenceCategory;)V"
        )
        wgf_txt = wgf_txt[: m.start()] + inject + wgf_txt[m.end() :]
    ws.write(wgf, wgf_txt)
    notes.append("WmGalleryFragment menu")

    # 4. Hook WatermarkItem sendDataToMivi (com/xiaomi/cam/watermark/a.m0) right before MiSys copy LRg/r;->c(v4, v2, p0)
    wma = next(p for p in ws.smali_files if str(p).endswith("/com/xiaomi/cam/watermark/a.smali"))
    wma_txt = ws.read(wma)
    if "OnyxPortHelper;->prepareMiviBrandLogos" not in wma_txt:
        pat_mivi = re.compile(
            r"(iget-boolean (p0|[pv]\d+), p0, Lcom/xiaomi/cam/watermark/a;->\w+:Z\s+)(invoke-static \{(v\d+), (v\d+), \2\}, L[^;]+;->\w+\(Ljava/nio/file/Path;Ljava/nio/file/Path;Z\)V)"
        )
        m = pat_mivi.search(wma_txt)
        if not m:
            return False, "Could not locate MiSys copy call in com/xiaomi/cam/watermark/a.smali"
        force_reg = m.group(2)
        src_reg = m.group(4)
        # v9 is dead right here (used earlier for "wmDirName" string check); reuse v9 safely!
        inject = (
            f"{m.group(1)}"
            f"invoke-static {{{src_reg}}}, Lcom/onyx/port/OnyxPortHelper;->prepareMiviBrandLogos(Ljava/nio/file/Path;)Z\n\n"
            f"    move-result v9\n\n"
            f"    or-int/2addr {force_reg}, v9\n\n"
            f"    {m.group(3)}"
        )
        ws.write(wma, wma_txt[: m.start()] + inject + wma_txt[m.end() :])
        notes.append("watermark/a MIVI logo sync")

    # 5. Add null-guard in WmGalleryPreference 1s clock runnable (ld/s.run) so getWaterMarkGroups()==null can never NPE
    tick_hits = ws.find_files_containing(
        "Lcom/android/camera/fragment/watermark/wmSettingV2/WmGalleryPreference;",
        f"{s['wm_mgr_cls']}->{s['wm_mgr_load_m']}(Z)Ljava/util/List;",
        "Ljava/util/ArrayList;->iterator()Ljava/util/Iterator;",
    )
    for tp in tick_hits:
        ttxt = ws.read(tp)
        if ":cond_onyx_nonnull_list" in ttxt:
            break
        pat_tick = re.compile(
            rf"(invoke-virtual \{{[^}}]+\}}, {re.escape(s['wm_mgr_cls'])}->{re.escape(s['wm_mgr_load_m'])}\(Z\)Ljava/util/List;\s+"
            r"move-result-object (v\d+)\s+check-cast \2, Ljava/util/ArrayList;\s+)"
            r"(invoke-virtual \{\2\}, Ljava/util/ArrayList;->iterator\(\)Ljava/util/Iterator;)"
        )
        mt = pat_tick.search(ttxt)
        if mt:
            lreg = mt.group(2)
            guard = (
                f"{mt.group(1)}"
                f"if-nez {lreg}, :cond_onyx_nonnull_list\n\n"
                f"    return-void\n\n"
                f"    :cond_onyx_nonnull_list\n"
                f"    {mt.group(3)}"
            )
            ws.write(tp, ttxt[: mt.start()] + guard + ttxt[mt.end() :])
            notes.append(f"null-guard in {tp.relative_to(ws.root)}")

    return True, "Installed Watermark brand switcher (POCO / 17 Ultra Leica / Leitzphone / Custom): " + ", ".join(notes)


# ---------------------------------------------------------------------------
# Group D: Surgical Capability Overrides on com.mi.device.Onyx
# ---------------------------------------------------------------------------

def resolve_feature_method(ws: SmaliWorkspace, feat_key: str):
    """
    Dynamically resolve each logical feature name to its exact Common method signature
    in the current APK build using JacocoIgnore keys + immutable caller anchors.
    """
    def from_jacoco(jkey: str, sig_suffix: str):
        cands = []
        for kind, sig in sorted(ws.jacoco_map.get(jkey, set())):
            if kind == "C" and sig.endswith(sig_suffix):
                cands.append(sig)
            elif kind == "W" and sig.endswith(sig_suffix):
                for cm in ws.resolve_wrapper_to_common(sig):
                    if cm.endswith(sig_suffix):
                        cands.append(cm)
        return list(dict.fromkeys(cands))

    if feat_key == "theme_cv_type":
        c = from_jacoco("!isSupportThemeCV", "()[I")
        return c[0] if c else None

    if feat_key == "support_cv_type":
        c = from_jacoco("isSupportCvType", "()Z")
        return c[0] if c else None

    if feat_key == "leica_watermark_support":
        # ParallelProcessProvider "watermark_leica_supported" -> Te/b.m2() -> Common.V2()
        c = ws.find_common_call_near_anchor('"watermark_leica_supported"', "()Z", file_hint="ParallelProcessProvider", window=600)
        return c[0] if c else None

    if feat_key == "default_shutter_sound_leica":
        # g2/c.smali has "key_shutter_sound" and "leica_mechanical" and calls Common->U()I
        c = ws.find_common_call_near_anchor('"leica_mechanical"', "()I", window=4000)
        return c[0] if c else None

    if feat_key == "high_dynamic_color_filter":
        c = from_jacoco("isSupportHighDynamicColorRepFromFilter", "()Z")
        return c[0] if c else None

    if feat_key == "support_legendary_mode":
        c = from_jacoco("isSupportLegendaryMode", "()Z")
        if not c:
            c = ws.find_common_call_near_anchor('"support Legenda Mode"', "()Z", window=600)
        return c[0] if c else None

    if feat_key == "legend_guide_and_capture_prefs":
        rid = ws.r_string.get("camera_guide_legend_module_title")
        if rid:
            c = ws.find_common_call_near_anchor(hex(rid), "()Z", window=1400, nezha_only=True)
            # Exclude support_legendary_mode (i5)
            leg = resolve_feature_method(ws, "support_legendary_mode")
            for x in c:
                if x != leg:
                    return x
            if c:
                return c[0]
        return None

    if feat_key == "smooth_zoom_3sat":
        c = from_jacoco("isSupport3SATZoomingOptimization", "()Z")
        return c[0] if c else None

    if feat_key in ("smooth_zoom_bar_v2_a", "smooth_zoom_bar_v2_b"):
        # Find Te/b method calling 3 bool methods on Common where 2 are nezha-only (B2, S8) and 1 is true (R8)
        wtxt = ws.read(ws.wrapper_path)
        for m in re.finditer(r"\.method public final (\w+)\(\)Z\n(.*?)\.end method", wtxt, re.S):
            body = m.group(2)
            calls = re.findall(re.escape(ws.common_obf) + r"->(\w+\(\)Z)", body)
            uniq = list(dict.fromkeys(calls))
            if len(uniq) == 3 and body.count(ws.common_obf) >= 5:
                nezha_only = [cm for cm in uniq if ws.is_nezha_only_bool(cm)]
                if len(nezha_only) == 2:
                    return nezha_only[0] if feat_key.endswith("_a") else nezha_only[1]
        return None

    if feat_key == "zoom_manager_flagship":
        c = ws.find_common_call_near_anchor('"setMfnr to "', "()Z", file_hint="StreetModule", window=1200, nezha_only=True)
        return c[0] if c else None

    if feat_key in ("equip_street_support_a", "equip_street_support_b"):
        c = [x for x in from_jacoco("isEquipStreetSupport", "()Z") if ws.is_nezha_only_bool(x)]
        if len(c) >= 2:
            return c[0] if feat_key.endswith("_a") else c[1]
        for kind, sig in ws.jacoco_map.get("isEquipStreetSupport", set()):
            if kind == "W":
                cs = [x for x in dict.fromkeys(ws.resolve_wrapper_to_common(sig)) if x.endswith("()Z") and ws.is_nezha_only_bool(x)]
                if len(cs) >= 2:
                    return cs[0] if feat_key.endswith("_a") else cs[1]
        return None

    if feat_key == "street_launch_support":
        c = ws.find_common_call_near_anchor('"android.media.action.STILL_IMAGE_CAMERA_SECURE"', "()Z", window=1500, nezha_only=True)
        return c[0] if c else None

    if feat_key == "pro_video_custom_lut":
        c = ws.find_common_call_near_anchor('"lut_film_warm"', "()Z", window=2000, nezha_only=True)
        return c[0] if c else None

    if feat_key == "pro_video_waveform_histogram":
        c = ws.find_common_call_near_anchor('"pref_camera_pro_video_waveform_graph"', "()Z", window=1200, nezha_only=True)
        return c[0] if c else None

    if feat_key == "movie_frame_239":
        c = ws.find_common_call_near_anchor('"2.39x1_new"', "()Z", window=1200, nezha_only=True)
        wm_m = resolve_feature_method(ws, "leica_watermark_support")
        for x in c:
            if x != wm_m:
                return x
        return c[0] if c else None

    if feat_key == "video_watermark_and_hdr":
        c = [x for x in from_jacoco("isSupportVideoWatermark", "()Z") if ws.is_nezha_only_bool(x)]
        return c[0] if c else None

    if feat_key == "true_colour_video":
        c = [x for x in from_jacoco("isTrueColourVideoSupported", "()Z") if ws.is_nezha_only_bool(x)]
        vw = resolve_feature_method(ws, "video_watermark_and_hdr")
        for x in c:
            if x != vw:
                return x
        return c[0] if c else None

    if feat_key == "cinematic_mode":
        c = ws.find_common_call_near_anchor("CinematicModuleEntry;", "()Z", file_hint="CinematicModuleEntry", window=4000, nezha_only=True)
        return c[0] if c else None

    if feat_key == "dual_video_mode":
        c = ws.find_common_call_near_anchor("DualCamModuleEntry;", "()Z", file_hint="DualCamModuleEntry", window=4000, nezha_only=True)
        return c[0] if c else None

    if feat_key == "custom_tint_color":
        c = ws.find_common_call_near_anchor('"pref_tint_color"', "()Z", window=2000, nezha_only=True)
        return c[0] if c else None

    if feat_key == "ultrapixel_capture_duration":
        c = from_jacoco("isSupportUltraPixelCaptureDuration", "()Z")
        return c[0] if c else None

    if feat_key == "motion_capture_status":
        c = ws.find_common_call_near_anchor('"pref_motion_capture_status"', "()Z", window=1200, nezha_only=True)
        return c[0] if c else None

    if feat_key == "super_jpeg_quality":
        c = ws.find_common_call_near_anchor('"pref_camera_video_mode_live_photo_state"', "()Z", window=2500, nezha_only=True)
        return c[0] if c else None

    if feat_key == "capture_method_prefs":
        c = ws.find_common_call_near_anchor("CaptureMethodFragment;", "()Z", file_hint="CaptureMethodFragment", window=8000, nezha_only=True)
        w5 = resolve_feature_method(ws, "legend_guide_and_capture_prefs")
        for x in c:
            if x != w5:
                return x
        return c[0] if c else None

    if feat_key == "master_lens_cvlens":
        c = ws.find_common_call_near_anchor('"get CvLens version from tag: "', "()I", window=1200)
        return c[0] if c else None

    return None


def make_smali_method_body(method_sig: str, ftype: str, value) -> str:
    header = f".method public final {method_sig}\n"
    if ftype == "bool":
        v = "0x1" if value else "0x0"
        return f"{header}    .locals 0\n\n    const/4 p0, {v}\n\n    return p0\n.end method\n"
    if ftype == "int":
        iv = int(value)
        if -8 <= iv <= 7:
            op = f"const/4 p0, {hex(iv)}"
        elif -32768 <= iv <= 32767:
            op = f"const/16 p0, {hex(iv)}"
        else:
            op = f"const p0, {hex(iv)}"
        return f"{header}    .locals 0\n\n    {op}\n\n    return p0\n.end method\n"
    if ftype == "int_array":
        arr = [int(x) for x in value]
        if len(arr) == 1:
            return (
                f"{header}    .locals 0\n\n"
                f"    const/16 p0, {hex(arr[0])}\n\n"
                f"    filled-new-array {{p0}}, [I\n\n"
                f"    move-result-object p0\n\n"
                f"    return-object p0\n"
                f".end method\n"
            )
        len_op = f"const/4 p0, {hex(len(arr))}" if len(arr) <= 7 else f"const/16 p0, {hex(len(arr))}"
        elems = "\n".join(f"        {hex(x)}" for x in arr)
        return (
            f"{header}    .locals 0\n\n"
            f"    {len_op}\n\n"
            f"    new-array p0, p0, [I\n\n"
            f"    fill-array-data p0, :array_0\n\n"
            f"    return-object p0\n\n"
            f"    nop\n\n"
            f"    :array_0\n"
            f"    .array-data 4\n"
            f"{elems}\n"
            f"    .end array-data\n"
            f".end method\n"
        )
    raise ValueError(f"Unsupported feature override type: {ftype}")


def apply_onyx_feature_overrides(ws: SmaliWorkspace, overrides: dict):
    """
    Inject or replace resolved capability methods directly inside com.mi.device.Onyx.
    Never modifies Common or other device classes; keeps all Onyx sensor/HAL methods intact.
    Wait: for methods that Common declares as `instance-of p0, p0, L<OtherDevice>;`
    and are NOT `final` in Common, overriding in Onyx works cleanly!
    We verify Common's method is not `final`; if `final`, we remove `final` on Common first.
    """
    onyx_path = ws.dec_to_path.get("com.mi.device.Onyx")
    common_path = ws.dec_to_path.get("com.mi.device.common.Common")
    if not onyx_path:
        raise RuntimeError("com.mi.device.Onyx not found in APK!")

    onyx_txt = ws.read(onyx_path)
    common_txt = ws.read(common_path)
    common_changed = False
    results = []

    for feat_key, spec in overrides.items():
        if not spec.get("enabled", True):
            results.append((feat_key, "SKIPPED", "disabled in profile"))
            continue
        method_sig = resolve_feature_method(ws, feat_key)
        if not method_sig:
            results.append((feat_key, "FAILED", "could not resolve Common method signature"))
            continue

        # Ensure superclass method in Common / CommonNote is not final
        m_name, m_rest = method_sig.split("(", 1)
        m_rest = "(" + m_rest
        final_pat = re.compile(rf"^(\.method public )final ({re.escape(m_name)}{re.escape(m_rest)})$", re.M)
        if final_pat.search(common_txt):
            common_txt = final_pat.sub(r"\1\2", common_txt)
            common_changed = True
        # Also check CommonNote (Onyx's direct superclass)
        cnote_path = ws.dec_to_path.get("com.mi.device.redmi.CommonNote")
        if cnote_path:
            cn_txt = ws.read(cnote_path)
            if final_pat.search(cn_txt):
                ws.write(cnote_path, final_pat.sub(r"\1\2", cn_txt))

        if feat_key == "smooth_zoom_3sat" and getattr(ws, "_onyx_port_helper_dir", None) is not None:
            new_method = (
                f".method public final {method_sig}\n"
                "    .locals 0\n\n"
                "    invoke-static {}, Lcom/onyx/port/OnyxPortHelper;->isSmoothZoomEnabled()Z\n\n"
                "    move-result p0\n\n"
                "    return p0\n"
                ".end method\n"
            )
        else:
            new_method = make_smali_method_body(method_sig, spec["type"], spec["value"])
        exist_pat = re.compile(
            rf"^\.method[^\n]* {re.escape(m_name)}{re.escape(m_rest)}\n.*?^\.end method\n?",
            re.M | re.S,
        )
        if exist_pat.search(onyx_txt):
            onyx_txt = exist_pat.sub(new_method, onyx_txt, count=1)
            action = "OVERRIDDEN"
        else:
            onyx_txt = onyx_txt.rstrip() + "\n\n" + new_method
            action = "INJECTED"
        results.append((feat_key, "OK", f"{action} Onyx->{method_sig} = {spec['value']} ({spec['desc']})"))

    ws.write(onyx_path, onyx_txt)
    if common_changed:
        ws.write(common_path, common_txt)
    return results


# ---------------------------------------------------------------------------
# Automatic DEX 64K Overflow Rebalancer (Update-Proof)
# ---------------------------------------------------------------------------

_RESERVED_TOP_PKGS = {
    "com", "android", "androidx", "miui", "miuix", "kotlin", "kotlinx",
    "java", "javax", "org", "io", "net", "okio", "okhttp3", "retrofit2",
    "google", "vendor", "dalvik", "sun", "META-INF",
}


def _count_dex_refs(smali_dir: Path) -> tuple[int, int, int]:
    """Count exact unique (method_ids, field_ids, type_ids) inside a smali* dir."""
    methods = set()
    fields = set()
    types = set()
    cls_re = re.compile(r"^\.class[^\n]+(L[^;\s]+;)", re.M)
    mdef_re = re.compile(r"^\.method[^\n]*? (\S+\([^)]*\)\S+)\s*$", re.M)
    fdef_re = re.compile(r"^\.field[^\n]*? (\S+:\S+)", re.M)
    mref_re = re.compile(r"(L[^;\s]+;->\S+\([^)]*\)\S+)")
    fref_re = re.compile(r"(L[^;\s]+;->[^\s(:]+:\S+)")
    type_re = re.compile(r"(L[^;\s]+;)")
    for f in smali_dir.rglob("*.smali"):
        t = f.read_text(encoding="utf-8", errors="ignore")
        mc = cls_re.search(t)
        cur = mc.group(1) if mc else ""
        if cur:
            types.add(cur)
            for md in mdef_re.findall(t):
                methods.add(f"{cur}->{md}")
            for fd in fdef_re.findall(t):
                fields.add(f"{cur}->{fd}")
        methods.update(mref_re.findall(t))
        fields.update(fref_re.findall(t))
        types.update(type_re.findall(t))
    return len(methods), len(fields), len(types)


def rebalance_dex_overflow(ws: SmaliWorkspace, limit: int = 62000) -> list[str]:
    """
    Guard against DEX 65,535 method/field/type ref overflow after smali injection.
    Inspects every smali* directory that received edits; if any table exceeds `limit`,
    cleanly relocates whole self-contained non-framework obfuscated package directories
    into a new smali_classes{N+1} folder so apktool/smali never overflows on any update.
    """
    notes = []
    # Find all smali dirs touched by patches
    touched_dirs = set()
    for p in ws.modified_paths:
        try:
            top = p.relative_to(ws.root).parts[0]
            if top.startswith("smali"):
                touched_dirs.add(ws.root / top)
        except Exception:
            pass

    if not touched_dirs:
        return notes

    # Determine next free smali_classesN index
    existing_nums = [1]
    for d in ws.root.glob("smali_classes*"):
        m = re.match(r"smali_classes(\d+)$", d.name)
        if m:
            existing_nums.append(int(m.group(1)))
    next_idx = max(existing_nums) + 1

    # Also build set of top-level subdirs containing modified files so we leave patched packages in place
    touched_subpkgs = set()
    for p in ws.modified_paths:
        rel = p.relative_to(ws.root).parts
        if len(rel) >= 2:
            touched_subpkgs.add((rel[0], rel[1]))

    for sdir in sorted(touched_dirs):
        m_cnt, f_cnt, t_cnt = _count_dex_refs(sdir)
        # Only rebalance when method_ids or type_ids genuinely approach 65535
        # (primary smali/ stays intact unless methods > 67000; R$* field tables in secondary dexes are stock-sized)
        is_primary = sdir.name == "smali"
        need_rebal = (m_cnt > 67000) if is_primary else (m_cnt > limit or t_cnt > limit)
        if not need_rebal:
            notes.append(f"{sdir.name}: OK (methods={m_cnt}, fields={f_cnt}, types={t_cnt})")
            continue

        # Collect candidate whole leaf obfuscated package directories to move
        cands = []
        for sub in sorted(sdir.iterdir()):
            if not sub.is_dir():
                continue
            if sub.name in _RESERVED_TOP_PKGS or (sdir.name, sub.name) in touched_subpkgs:
                continue
            fcnt = sum(1 for _ in sub.rglob("*.smali"))
            if 15 <= fcnt <= 900:
                cands.append((fcnt, sub))
        # Prefer medium-sized self-contained packages first
        cands.sort(key=lambda x: x[0], reverse=True)

        overflow_dir = ws.root / f"smali_classes{next_idx}"
        overflow_dir.mkdir(exist_ok=True)
        next_idx += 1

        moved_pkgs = []
        moved_files = 0
        # Move packages until comfortably under limit (target ~58k)
        for fcnt, sub in cands:
            dst = overflow_dir / sub.name
            shutil.move(str(sub), str(dst))
            moved_pkgs.append(f"{sub.name}({fcnt})")
            moved_files += fcnt
            if moved_files >= 450:
                m2, f2, t2 = _count_dex_refs(sdir)
                if max(m2, f2, t2) <= limit - 3000:
                    notes.append(
                        f"{sdir.name}: rebalanced {m_cnt}->{m2} methods (moved {moved_files} files "
                        f"[{', '.join(moved_pkgs)}] -> {overflow_dir.name})"
                    )
                    break
        else:
            m2, f2, t2 = _count_dex_refs(sdir)
            notes.append(
                f"{sdir.name}: rebalanced {m_cnt}->{m2} methods (moved {moved_files} files -> {overflow_dir.name})"
            )

    return notes


# ---------------------------------------------------------------------------
# Version suffix tagging in apktool.yml
# ---------------------------------------------------------------------------

def patch_version_suffix(root: Path, suffix: str):
    if not suffix:
        return True, "No version suffix configured"
    yml = root / "apktool.yml"
    if not yml.exists():
        return False, "apktool.yml not found"
    txt = yml.read_text(encoding="utf-8")
    m = re.search(r"^(\s*versionName:\s*)(\S+)\s*$", txt, re.M)
    if not m:
        return False, "versionName not found in apktool.yml"
    cur = m.group(2).strip("'\"")
    if cur.endswith(suffix):
        return True, f"versionName already tagged: {cur}"
    new_ver = f"{cur}{suffix}"
    new_txt = txt[: m.start()] + f"{m.group(1)}{new_ver}" + txt[m.end() :]
    yml.write_text(new_txt, encoding="utf-8")
    return True, f"versionName: {cur} -> {new_ver}"


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

PATCH_FUNCS = {
    "compat_global_mod_device": patch_compat_global_mod_device,
    "compat_disable_safe_check": patch_compat_disable_safe_check,
    "compat_obfu_res_fallback": patch_compat_obfu_res_fallback,
    "compat_stub_jcodec": patch_compat_stub_jcodec,
    "compat_skip_nal_length": patch_compat_skip_nal_length,
    "compat_google_photos_gallery": patch_compat_google_photos_gallery,
    "compat_fresh_thumbnail": patch_compat_fresh_thumbnail,
    "compat_leica_limited_toast": patch_compat_leica_limited_toast,
    "compat_fix_50mp_blob_buffer": patch_compat_fix_50mp_blob_buffer,
    "compat_fix_ultrahdr_when_hdr_off": patch_compat_fix_ultrahdr_when_hdr_off,
    "leitz_force_lcc_theme": patch_leitz_force_lcc_theme,
    "leica_cloud_filter_device": patch_leica_cloud_filter_device,
    "leica_cloud_filter_custom_json": patch_leica_cloud_filter_custom_json,
    "smooth_zoom_menu_and_default": patch_smooth_zoom_menu_and_default,
    "camera_pro_mod_menu": patch_camera_pro_mod_menu,
    "watermark_unlock_all": patch_watermark_unlock_all,
    "watermark_fix_missing_poco_logos": patch_watermark_fix_missing_poco_logos,
    "watermark_fix_gallery_lag": patch_watermark_fix_gallery_lag,
    "watermark_brand_switcher_menu": patch_watermark_brand_switcher_menu,
}


def main():
    ap = argparse.ArgumentParser(description="Self-healing MiuiCamera Nezha->Onyx patcher")
    ap.add_argument("decompiled_dir", type=Path, help="Path to apktool-decompiled MiuiCamera dir")
    ap.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "config" / "port_profile.json",
        help="Path to port_profile.json",
    )
    ap.add_argument("--report", type=Path, default=None, help="Optional JSON report output path")
    ap.add_argument("--strict", action="store_true", help="Exit non-zero if any enabled patch fails")
    args = ap.parse_args()

    profile = json.loads(args.config.read_text(encoding="utf-8"))
    print(f"[patch_camera] Indexing {args.decompiled_dir} ...", flush=True)
    ws = SmaliWorkspace(args.decompiled_dir)
    print(
        f"[patch_camera] Discovered Common={ws.common_obf}, Wrapper={ws.wrapper_cls}, "
        f"Onyx={ws.dec_to_obf.get('com.mi.device.Onyx')}, "
        f"R$string={len(ws.r_string)}, JacocoKeys={len(ws.jacoco_map)}",
        flush=True,
    )

    report = {"profile": profile.get("profile_name"), "patches": [], "feature_overrides": []}
    failures = []

    for section in ("compat_patches", "leica_leitzphone_patches", "watermark_patches"):
        print(f"\n=== {section} ===", flush=True)
        for pname, enabled in profile.get(section, {}).items():
            if not enabled:
                print(f"  [SKIP] {pname}", flush=True)
                report["patches"].append({"name": pname, "status": "SKIPPED", "detail": "disabled"})
                continue
            fn = PATCH_FUNCS.get(pname)
            if not fn:
                print(f"  [FAIL] {pname}: unknown patch function", flush=True)
                failures.append(pname)
                continue
            ok, detail = fn(ws)
            status = "OK" if ok else "FAILED"
            print(f"  [{status:4s}] {pname:34s} -> {detail}", flush=True)
            report["patches"].append({"name": pname, "status": status, "detail": detail})
            if not ok:
                failures.append(pname)

    print("\n=== onyx_feature_overrides ===", flush=True)
    ov_results = apply_onyx_feature_overrides(ws, profile.get("onyx_feature_overrides", {}))
    for fkey, status, detail in ov_results:
        print(f"  [{status:4s}] {fkey:34s} -> {detail}", flush=True)
        report["feature_overrides"].append({"name": fkey, "status": status, "detail": detail})
        if status == "FAILED":
            failures.append(fkey)

    ok_v, det_v = patch_version_suffix(args.decompiled_dir, profile.get("version_suffix", ""))
    print(f"\n=== version_suffix ===\n  [{'OK' if ok_v else 'FAIL'}] {det_v}", flush=True)

    print("\n=== dex_rebalance ===", flush=True)
    rebal_notes = rebalance_dex_overflow(ws)
    for n in rebal_notes:
        print(f"  [OK  ] {n}", flush=True)
    report["dex_rebalance"] = rebal_notes

    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        print(f"\n[patch_camera] Report saved to {args.report}", flush=True)

    if failures:
        print(f"\n[patch_camera] WARNING: {len(failures)} patch(es) failed: {failures}", flush=True)
        if args.strict:
            sys.exit(1)
    else:
        print("\n[patch_camera] ALL ENABLED PATCHES SUCCEEDED CLEANLY!", flush=True)


if __name__ == "__main__":
    main()
