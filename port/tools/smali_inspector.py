#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Obfuscation-Resilient Smali Inspector & Reverse-Engineering Toolkit for MiuiCamera.

Makes reading, searching, deobfuscating, diffing, and verifying obfuscated MiuiCamera
APKs fast and painless — both for active debugging and future HyperOS camera updates.

Subcommands:
  summary                    Show discovered Core classes (Common, Onyx, Nezha, Wrapper, R$string)
  devices [--filter REGEX]   List all XOR-decrypted com.mi.device.* classes & inheritance
  diff <DevA> <DevB> [--filter REGEX]
                             Diff capability methods between two devices (e.g. Onyx Nezha)
  find <needle> [--all] [--context N]
                             Search strings/tags/Jacoco keys across smali with enclosing method
                             + auto-annotated Common/Wrapper calls
  xref <target> [--context N] [--limit N]
                             Find all callers/references to a class or method (accepts obfuscated
                             LTe/b;->V()Z OR decrypted Common->u4()Z)
  dump <target> [method] [--lines A:B]
                             Print a class, method, or line range with inline deobfuscation
                             (/* Common->foo() */, /* @string/... = "..." */, /* xor="..." */)
  res <query_or_hex> [--callers]
                             Search res/values/strings.xml + public.xml + R$string by keyword or hex ID
  props [FILTER] [--limit N] Scan system properties (ro.*, vendor.*, persist.*, camera.*, debug.*)
  jacoco [FILTER]            Search JacocoIgnore keys with Onyx vs Nezha capability resolution
  vtags [FILTER] [--callers] Search Camera2 vendor tag strings and resolve their static tag fields
  field-writers <Cls->field> Find every sput/iput writing to a field across smali
  prefs <FragmentOrClass>    Summarize preference keys, categories, and Preference methods in a class
  inspect-jpg <file...>      Safe Python-only JPEG/50MP/MotionPhoto/UltraHDR container inspector
  dex-stats                  Exact DEX index pressure (methods/fields/types/strings) per smali* dir
  check-profile [-c PROFILE] Dry-run every resolver in patch_camera.py & print full symbol map
"""

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
PORT_ROOT = TOOLS_DIR.parent
_DEFAULT_DEC_CANDS = [
    PORT_ROOT / "work" / "decompiled_nezha",
    Path("/home/ireddragonicy/Data/projects/onyx/nezha-camera-port/work/decompiled_nezha"),
]
DEFAULT_DEC = next((p for p in _DEFAULT_DEC_CANDS if p.exists()), _DEFAULT_DEC_CANDS[0])
DEFAULT_PROFILE = PORT_ROOT / "config" / "port_profile.json"

sys.path.insert(0, str(TOOLS_DIR))
import patch_camera as pc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_workspace(dec_dir: Path) -> pc.SmaliWorkspace:
    if not dec_dir.exists():
        raise SystemExit(f"[inspector] Decompiled dir not found: {dec_dir}")
    return pc.SmaliWorkspace(dec_dir)


def build_reverse_maps(ws: pc.SmaliWorkspace):
    """Build helpful reverse lookup maps for inline deobfuscation."""
    # R$string hex -> name
    id_to_str = {val: name for name, val in ws.r_string.items()}

    # Common method -> jacoco keys & wrapper methods
    common_to_jacoco = defaultdict(set)
    wrapper_to_jacoco = defaultdict(set)
    for k, items in ws.jacoco_map.items():
        for kind, sig in items:
            if kind == "C":
                common_to_jacoco[sig].add(k)
            else:
                wrapper_to_jacoco[sig].add(k)

    # Wrapper method -> common methods called inside it
    wrapper_to_common = {}
    wtxt = ws.read(ws.wrapper_path)
    for m in re.finditer(r"^\.method[^\n]*? (\S+?)\(([^)]*)\)(\S+)\n(.*?)^\.end method", wtxt, re.M | re.S):
        wsig = f"{m.group(1)}({m.group(2)}){m.group(3)}"
        ccalls = list(dict.fromkeys(re.findall(re.escape(ws.common_obf) + r"->(\S+\([^)]*\)\S+)", m.group(4))))
        if ccalls:
            wrapper_to_common[wsig] = ccalls
            for cs in ccalls:
                for jk in wrapper_to_jacoco.get(wsig, ()):
                    common_to_jacoco[cs].add(jk)

    return id_to_str, common_to_jacoco, wrapper_to_jacoco, wrapper_to_common


def load_string_values(dec_dir: Path) -> dict[int, tuple[str, str]]:
    """Map resource int id -> (public_name, text_value) from public.xml + strings.xml."""
    pub_path = dec_dir / "res" / "values" / "public.xml"
    str_path = dec_dir / "res" / "values" / "strings.xml"
    if not pub_path.exists() or not str_path.exists():
        return {}
    name_to_val = {}
    for m in re.finditer(r'<string name="([^"]+)">([^<]*)</string>', str_path.read_text(errors="ignore")):
        name_to_val[m.group(1)] = m.group(2)
    out = {}
    for m in re.finditer(r'type="string" name="([^"]+)" id="(0x[0-9a-fA-F]+)"', pub_path.read_text(errors="ignore")):
        nm = m.group(1)
        out[int(m.group(2), 16)] = (nm, name_to_val.get(nm, ""))
    return out


def _try_decode_xor_literal(escaped: str, xor_key: int) -> str | None:
    try:
        raw = bytes(escaped, "utf-8").decode("unicode_escape")
    except Exception:
        return None
    if not raw or all(ord(c) < 128 for c in raw):
        return None
    k16 = xor_key & 0xFFFF
    dec = "".join(chr(ord(c) ^ k16) for c in raw)
    if all(32 <= ord(c) < 127 for c in dec):
        return dec
    return None


def annotate_smali(text: str, ws: pc.SmaliWorkspace, rev_maps=None) -> str:
    """Return smali text with inline human-readable deobfuscation comments."""
    if rev_maps is None:
        rev_maps = build_reverse_maps(ws)
    id_to_str, common_to_jacoco, wrapper_to_jacoco, wrapper_to_common = rev_maps
    str_vals = getattr(ws, "_str_vals_cache", None)
    if str_vals is None:
        str_vals = load_string_values(ws.root)
        ws._str_vals_cache = str_vals

    # Detect any XOR key used with LC9/n;->k(ILjava/lang/String;) or similar const vX, 0x...
    xor_keys = [int(x, 16) for x in re.findall(r"const [pv]\d+, (0x[0-9a-fA-F]{5,8})", text)]
    if not xor_keys:
        xor_keys = [0x22A3D030]

    out_lines = []
    obf_pat = re.compile(r"(L[^\x00-\x7f][^;\s]*;)(?:->(\S+\([^)]*\)\S+))?")
    wrap_pat = re.compile(re.escape(ws.wrapper_cls) + r"->(\S+\([^)]*\)\S+)")
    hex_pat = re.compile(r"\b(0x7f[0-9a-fA-F]{6})\b")
    uni_str_pat = re.compile(r'"((?:\\u[0-9a-fA-F]{4})+)"')

    for line in text.splitlines():
        notes = []
        for m in obf_pat.finditer(line):
            cls_obf, msig = m.group(1), m.group(2)
            dec = ws.obf_to_dec.get(cls_obf) or pc.decode_device_cls(cls_obf)
            short_dec = dec.rsplit(".", 1)[-1]
            if msig:
                jks = sorted(common_to_jacoco.get(msig, ()))
                ox = ws.eval_bool_on_device("com.mi.device.Onyx", msig) if msig.endswith("()Z") else None
                nz = ws.eval_bool_on_device("com.mi.device.Nezha", msig) if msig.endswith("()Z") else None
                extra = []
                if jks:
                    extra.append("jacoco=" + ",".join(jks[:2]))
                if ox is not None or nz is not None:
                    extra.append(f"Onyx={ox}/Nezha={nz}")
                notes.append(f"{short_dec}->{msig}" + (f" [{' ; '.join(extra)}]" if extra else ""))
            else:
                notes.append(dec)

        for m in wrap_pat.finditer(line):
            wsig = m.group(1)
            jks = sorted(wrapper_to_jacoco.get(wsig, ()))
            cc = wrapper_to_common.get(wsig, [])
            parts = []
            if cc:
                parts.append("calls:" + ",".join(cc[:3]))
            if jks:
                parts.append("jacoco=" + ",".join(jks[:2]))
            notes.append(f"DataItemFeature->{wsig}" + (f" [{' ; '.join(parts)}]" if parts else ""))

        for m in hex_pat.finditer(line):
            rid = int(m.group(1), 16)
            rn = id_to_str.get(rid)
            sv = str_vals.get(rid)
            if rn or sv:
                label = f"@string/{rn}" if rn else f"@{sv[0]}"
                if sv and sv[1]:
                    label += f' = "{sv[1][:60]}"'
                notes.append(label)

        for m in uni_str_pat.finditer(line):
            for xk in xor_keys:
                dec_s = _try_decode_xor_literal(m.group(1), xk)
                if dec_s:
                    notes.append(f'xor={dec_s!r}')
                    break

        if notes:
            out_lines.append(f"{line}    # " + " | ".join(dict.fromkeys(notes)))
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def resolve_class_target(ws: pc.SmaliWorkspace, target: str) -> tuple[str, Path]:
    """Accept decrypted name ('Onyx', 'com.mi.device.Onyx', 'DataItemFeature'), simple class name, L...;, or rel path."""
    if target in ("DataItemFeature", "Wrapper"):
        return ws.wrapper_cls, ws.wrapper_path

    # Direct path check
    cand = ws.root / target
    if cand.exists() and cand.is_file():
        txt = ws.read(cand)
        mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", txt, re.M)
        return (mc.group(1) if mc else target), cand

    # Decrypted device class (full or short name)
    if target in ws.dec_to_path:
        return ws.dec_to_obf[target], ws.dec_to_path[target]
    for dec, p in ws.dec_to_path.items():
        if dec.rsplit(".", 1)[-1].lower() == target.lower():
            return ws.dec_to_obf[dec], p

    # Smali descriptor e.g. LTe/b; or Te/b or com.android.camera.Camera
    norm = target.strip()
    if norm.startswith("L") and norm.endswith(";"):
        rel = norm[1:-1] + ".smali"
    else:
        rel = norm.replace(".", "/")
        if not rel.endswith(".smali"):
            rel += ".smali"

    for smali_dir in sorted(ws.root.glob("smali*")):
        p = smali_dir / rel
        if p.exists():
            return "L" + rel[:-6] + ";", p

    # Fallback: match by basename (e.g. "WmGalleryFragment" or "MockCameraImageReceiver")
    base = target.rsplit("/", 1)[-1].rsplit(".", 1)[-1]
    want = base if base.endswith(".smali") else f"{base}.smali"
    for p in ws.smali_files:
        if p.name == want:
            txt = ws.read(p)
            mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", txt, re.M)
            return (mc.group(1) if mc else target), p

    raise SystemExit(f"[inspector] Could not resolve class target: {target}")


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def cmd_summary(ws: pc.SmaliWorkspace, _args):
    print(f"Decompiled root : {ws.root}")
    print(f"Total .smali    : {len(ws.smali_files)}")
    print(f"Obf device cls  : {len(ws.obf_to_dec)}")
    print(f"Common          : {ws.common_obf}  ({ws.dec_to_path['com.mi.device.common.Common'].relative_to(ws.root)})")
    print(f"Onyx            : {ws.dec_to_obf.get('com.mi.device.Onyx')}  ({ws.dec_to_path.get('com.mi.device.Onyx', Path()).relative_to(ws.root) if 'com.mi.device.Onyx' in ws.dec_to_path else 'N/A'})")
    print(f"Nezha           : {ws.dec_to_obf.get('com.mi.device.Nezha')}  ({ws.dec_to_path.get('com.mi.device.Nezha', Path()).relative_to(ws.root) if 'com.mi.device.Nezha' in ws.dec_to_path else 'N/A'})")
    print(f"DataItemFeature : {ws.wrapper_cls}  ({ws.wrapper_path.relative_to(ws.root)})")
    print(f"R$string entries: {len(ws.r_string)}")
    print(f"Jacoco keys     : {len(ws.jacoco_map)}")


def cmd_devices(ws: pc.SmaliWorkspace, args):
    pat = re.compile(args.filter, re.I) if args.filter else None
    for dec in sorted(ws.dec_to_obf.keys()):
        if pat and not pat.search(dec):
            continue
        obf = ws.dec_to_obf[dec]
        sup = ws.cls_super.get(obf, "")
        sup_dec = ws.obf_to_dec.get(sup, sup)
        mcnt = len(ws.cls_methods.get(obf, {}))
        rel = ws.dec_to_path[dec].relative_to(ws.root)
        print(f"{dec:45s} extends {sup_dec:35s} ({mcnt:3d} methods)  [{rel}]")


def eval_simple_value(ws: pc.SmaliWorkspace, dec_name: str, msig: str):
    chain = ws._device_chain(dec_name)
    chain_set = set(chain)
    for c in chain:
        body = ws.cls_methods.get(c, {}).get(msig)
        if body is None:
            continue
        definer = ws.obf_to_dec.get(c, c).rsplit(".", 1)[-1]
        lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith((".", "#", ":"))]
        if len(lines) == 2 and lines[1].startswith("return"):
            m = re.match(r"const(?:/4|/16|/high16)?\s+[pv]\d+,\s*(-?0x[0-9a-fA-F]+|-?\d+)", lines[0])
            if m:
                val = int(m.group(1), 0)
                if msig.endswith("()Z"):
                    return definer, str(bool(val))
                return definer, f"{val} ({hex(val)})"
            ms = re.match(r'const-string(?:/jumbo)?\s+[pv]\d+,\s*(".*")', lines[0])
            if ms:
                return definer, ms.group(1)
        if "instance-of" in body and msig.endswith("()Z"):
            b = ws.eval_bool_on_device(dec_name, msig)
            if b is not None:
                return definer, f"{b} (instance-of)"
        return definer, "<complex: " + " ; ".join(lines[:3])[:60] + ">"
    return "-", "<missing>"


def cmd_diff(ws: pc.SmaliWorkspace, args):
    def resolve_dev(name):
        if name in ws.dec_to_obf:
            return name
        for d in ws.dec_to_obf:
            if d.rsplit(".", 1)[-1].lower() == name.lower():
                return d
        raise SystemExit(f"Unknown device class: {name}")

    dev_a = resolve_dev(args.dev_a)
    dev_b = resolve_dev(args.dev_b)
    _, common_to_jacoco, _, _ = build_reverse_maps(ws)
    all_sigs = sorted(ws.cls_methods.get(ws.common_obf, {}).keys())
    pat = re.compile(args.filter, re.I) if args.filter else None

    diffs = 0
    for sig in all_sigs:
        if sig.startswith("<"):
            continue
        da, va = eval_simple_value(ws, dev_a, sig)
        db, vb = eval_simple_value(ws, dev_b, sig)
        if va == vb:
            continue
        jks = sorted(common_to_jacoco.get(sig, ()))
        line = f"{sig:22s} | {args.dev_a}={va:20s} ({da:14s}) | {args.dev_b}={vb:20s} ({db:14s}) | jacoco={','.join(jks[:2])}"
        if pat and not pat.search(line):
            continue
        print(line)
        diffs += 1
    print(f"\nTotal diffs shown: {diffs}")


def cmd_find(ws: pc.SmaliWorkspace, args):
    needle = args.needle
    rev_maps = build_reverse_maps(ws)
    _, common_to_jacoco, wrapper_to_jacoco, wrapper_to_common = rev_maps
    method_pat = re.compile(r"^\.method[^\n]*\n(.*?)^\.end method", re.M | re.S)
    hits = 0

    for p in ws.smali_files:
        if not args.all and any(ord(c) > 127 for c in p.name):
            continue
        txt = ws.read(p)
        if needle not in txt:
            continue
        rel = p.relative_to(ws.root)
        matched_method = False
        for m in method_pat.finditer(txt):
            block = m.group(0)
            if needle not in block:
                continue
            matched_method = True
            header = block.splitlines()[0]
            # Extract Common & Wrapper calls in this method
            ccalls = list(dict.fromkeys(re.findall(re.escape(ws.common_obf) + r"->(\S+\([^)]*\)\S+)", block)))
            wcalls = list(dict.fromkeys(re.findall(re.escape(ws.wrapper_cls) + r"->(\S+\([^)]*\)\S+)", block)))
            print(f"[{rel}] {header}")
            if ccalls:
                ann = [f"{c} (Onyx={ws.eval_bool_on_device('com.mi.device.Onyx', c)}/Nezha={ws.eval_bool_on_device('com.mi.device.Nezha', c)})"
                       if c.endswith("()Z") else c for c in ccalls[:6]]
                print(f"    Common calls : {', '.join(ann)}")
            if wcalls:
                ann_w = [f"{w}->{wrapper_to_common.get(w, [])}" for w in wcalls[:6]]
                print(f"    Wrapper calls: {', '.join(ann_w)}")
            if args.context > 0:
                lines = block.splitlines()
                for idx, ln in enumerate(lines):
                    if needle in ln:
                        lo = max(0, idx - args.context)
                        hi = min(len(lines), idx + args.context + 1)
                        snippet = annotate_smali("\n".join(lines[lo:hi]), ws, rev_maps)
                        for sl in snippet.splitlines():
                            print(f"      {sl}")
                        break
            hits += 1
            if hits >= args.limit:
                print(f"... truncated at --limit {args.limit}")
                return
        if not matched_method:
            print(f"[{rel}] (class-level match)")
            hits += 1
            if hits >= args.limit:
                print(f"... truncated at --limit {args.limit}")
                return
    print(f"\nTotal matches: {hits}")


def cmd_xref(ws: pc.SmaliWorkspace, args):
    target = args.target
    # Allow "Common->u4()Z" or "DataItemFeature->V()Z"
    if "->" in target:
        cls_part, meth_part = target.split("->", 1)
        cls_desc, _ = resolve_class_target(ws, cls_part)
        needle = f"{cls_desc}->{meth_part}"
    else:
        cls_desc, _ = resolve_class_target(ws, target)
        needle = cls_desc
    print(f"Searching xrefs for: {needle}\n")
    args.needle = needle
    args.all = True
    cmd_find(ws, args)


def cmd_dump(ws: pc.SmaliWorkspace, args):
    cls_desc, p = resolve_class_target(ws, args.target)
    txt = ws.read(p)
    rev_maps = build_reverse_maps(ws)
    print(f"# File: {p.relative_to(ws.root)}  ({cls_desc})\n")
    if args.lines:
        parts = args.lines.split(":", 1)
        lo = max(1, int(parts[0]))
        hi = int(parts[1]) if len(parts) > 1 and parts[1] else lo + 120
        raw_lines = txt.splitlines()
        sliced = "\n".join(raw_lines[lo - 1 : hi])
        for idx, ln in enumerate(annotate_smali(sliced, ws, rev_maps).splitlines(), lo):
            print(f"{idx:5d}: {ln}")
        return
    if args.method:
        pat = re.compile(rf"^\.method[^\n]* {re.escape(args.method)}[^\n]*\n.*?^\.end method", re.M | re.S)
        matches = list(pat.finditer(txt))
        if not matches:
            raise SystemExit(f"[inspector] Method '{args.method}' not found in {p.relative_to(ws.root)}")
        for m in matches:
            print(annotate_smali(m.group(0), ws, rev_maps))
            print()
    else:
        lines = annotate_smali(txt, ws, rev_maps).splitlines()
        for ln in lines[: args.max_lines]:
            print(ln)
        if len(lines) > args.max_lines:
            print(f"\n... ({len(lines) - args.max_lines} more lines; pass a method name, --lines A:B, or --max-lines)")


def cmd_res(dec_dir: Path, args):
    str_vals = load_string_values(dec_dir)
    q = args.query.strip()
    ws = load_workspace(dec_dir) if args.callers else None
    id_to_rname = {v: k for k, v in ws.r_string.items()} if ws else {}

    matches = []
    if re.fullmatch(r"0x[0-9a-fA-F]+", q):
        rid = int(q, 16)
        if rid in str_vals:
            matches.append((rid, str_vals[rid][0], str_vals[rid][1]))
    else:
        pat = re.compile(q, re.I)
        for rid, (pub_name, val) in sorted(str_vals.items()):
            rn = id_to_rname.get(rid, "")
            if pat.search(val) or pat.search(pub_name) or (rn and pat.search(rn)):
                matches.append((rid, pub_name, val))

    for rid, pub_name, val in matches[: args.limit]:
        hx = hex(rid)
        rn = id_to_rname.get(rid, "")
        rn_str = f" R.string.{rn}" if rn else ""
        print(f"  {hx} ({pub_name}){rn_str}: {val[:110]}")
        if ws and args.callers:
            for p in ws.smali_files:
                if any(ord(c) > 127 for c in p.name):
                    continue
                t = ws.read(p)
                if hx in t and t.count(".field public static final") < 50:
                    print(f"      -> caller: {p.relative_to(ws.root)}")
    print(f"\nTotal matches: {len(matches)} (shown {min(len(matches), args.limit)})")


def cmd_field_writers(ws: pc.SmaliWorkspace, args):
    target = args.target
    if "->" in target:
        cls_part, fld_part = target.split("->", 1)
        cls_desc, _ = resolve_class_target(ws, cls_part)
        needle = f"{cls_desc}->{fld_part}"
    else:
        needle = target
    rev_maps = build_reverse_maps(ws)
    pat = re.compile(r"^\s*(sput|iput)[^\n]*" + re.escape(needle))
    hits = 0
    for p in ws.smali_files:
        if any(ord(c) > 127 for c in p.name):
            continue
        txt = ws.read(p)
        if needle not in txt:
            continue
        lines = txt.splitlines()
        for i, ln in enumerate(lines, 1):
            if pat.search(ln):
                print(f"[{p.relative_to(ws.root)}:{i}]")
                lo = max(0, i - 1 - args.context)
                hi = min(len(lines), i + args.context)
                for j, aln in enumerate(annotate_smali("\n".join(lines[lo:hi]), ws, rev_maps).splitlines(), lo + 1):
                    mark = ">>" if j == i else "  "
                    print(f"  {mark} {j:5d}: {aln}")
                hits += 1
                if hits >= args.limit:
                    print(f"... truncated at --limit {args.limit}")
                    return
    print(f"\nTotal field writers found: {hits}")


def cmd_prefs(ws: pc.SmaliWorkspace, args):
    cls_desc, p = resolve_class_target(ws, args.target)
    txt = ws.read(p)
    print(f"# Preferences in {p.relative_to(ws.root)} ({cls_desc})\n")
    fields = re.findall(r"^\.field[^\n]+$", txt, re.M)
    if fields:
        print("Fields:")
        for f in fields:
            print(f"  {f}")
    keys = sorted(set(re.findall(r'"((?:pref_|category_|key_)[^"]+)"', txt)))
    print(f"\nPref/Category keys ({len(keys)}):")
    for k in keys:
        print(f"  {k}")
    methods = re.findall(r"^\.method[^\n]+$", txt, re.M)
    print(f"\nMethods ({len(methods)}):")
    for m in methods:
        print(f"  {m}")


def cmd_props(ws: pc.SmaliWorkspace, args):
    """Scan all system property keys across smali with enclosing method and nearby default."""
    pat = re.compile(args.filter, re.I) if args.filter else None
    prop_re = re.compile(r'const-string(?:/jumbo)?\s+[pv]\d+,\s*"((?:ro|persist|vendor|debug|camera|miui|sys|cam)\.[A-Za-z0-9_.-]+)"')
    hits = defaultdict(list)
    for p in ws.smali_files:
        if any(ord(c) > 127 for c in p.name):
            continue
        txt = ws.read(p)
        if not prop_re.search(txt):
            continue
        rel = str(p.relative_to(ws.root))
        lines = txt.splitlines()
        for i, ln in enumerate(lines):
            for m in prop_re.finditer(ln):
                pr = m.group(1)
                if pat and not pat.search(pr):
                    continue
                if len(hits[pr]) < 4:
                    m_hdr = ""
                    for j in range(i, max(-1, i - 100), -1):
                        if lines[j].startswith(".method "):
                            m_hdr = lines[j].strip().split()[-1]
                            break
                    hits[pr].append(f"{rel}:{i + 1} ({m_hdr})")
    shown = 0
    for pr in sorted(hits):
        print(f"  {pr}")
        for loc in hits[pr]:
            print(f"      -> {loc}")
        shown += 1
        if shown >= args.limit:
            print(f"... truncated at --limit {args.limit} (total {len(hits)})")
            return
    print(f"\nTotal matching properties: {len(hits)}")


def cmd_jacoco(ws: pc.SmaliWorkspace, args):
    """Search JacocoIgnore keys with Onyx vs Nezha resolution."""
    pat = re.compile(args.filter, re.I) if args.filter else None
    shown = 0
    for k in sorted(ws.jacoco_map):
        if pat and not pat.search(k):
            continue
        parts = []
        for kind, sig in sorted(ws.jacoco_map[k]):
            if kind == "C":
                _, ov = eval_simple_value(ws, "com.mi.device.Onyx", sig)
                _, nv = eval_simple_value(ws, "com.mi.device.Nezha", sig)
                parts.append(f"C:{sig}(O={ov}, N={nv})")
            else:
                cms = ws.resolve_wrapper_to_common(sig)
                sub = []
                for cm in cms:
                    _, ov = eval_simple_value(ws, "com.mi.device.Onyx", cm)
                    _, nv = eval_simple_value(ws, "com.mi.device.Nezha", cm)
                    sub.append(f"{cm}(O={ov},N={nv})")
                sub_s = ", ".join(sub) if sub else "no-common"
                parts.append(f"W:{sig}->[{sub_s}]")
        print(f"  {k:44s} : {' ; '.join(parts)}")
        shown += 1
        if shown >= args.limit:
            print(f"... truncated at --limit {args.limit}")
            break
    print(f"\nTotal Jacoco keys shown: {shown}")


def cmd_vtags(ws: pc.SmaliWorkspace, args):
    """Search Camera2 vendor tag strings and map to their supplier/holder fields."""
    pat = re.compile(args.filter, re.I) if args.filter else None
    tag_re = re.compile(r'const-string(?:/jumbo)?\s+[pv]\d+,\s*"((?:com\.xiaomi|xiaomi|org\.quic|com\.mi\.)[A-Za-z0-9_.-]+)"')
    found = []
    for p in ws.smali_files:
        if any(ord(c) > 127 for c in p.name):
            continue
        txt = ws.read(p)
        if not ("com.xiaomi." in txt or "xiaomi." in txt or "org.quic." in txt or "com.mi." in txt):
            continue
        for m in tag_re.finditer(txt):
            tag = m.group(1)
            if pat and not pat.search(tag):
                continue
            found.append((tag, str(p.relative_to(ws.root)), m.start()))
    seen = set()
    shown = 0
    for tag, rel, _ in sorted(found):
        if (tag, rel) in seen:
            continue
        seen.add((tag, rel))
        print(f"  {tag:68s} [{rel}]")
        shown += 1
        if shown >= args.limit:
            print(f"... truncated at --limit {args.limit}")
            break
    print(f"\nTotal vendor tags shown: {shown}")


def _parse_jpeg_header_info(raw: bytes) -> dict:
    info = {
        "size_bytes": len(raw),
        "valid_soi": raw[:2] == b"\xff\xd8",
        "valid_eoi": raw.endswith(b"\xff\xd9") or (b"\xff\xd9" in raw[-64:]),
        "width": None,
        "height": None,
        "exif_strings": [],
        "xmp_xml": "",
        "has_mpf": False,
        "has_gainmap": False,
        "has_motion_photo": False,
        "mp4_offset": None,
        "mp4_size": 0,
    }
    i = 2
    n = len(raw)
    while i + 4 <= n:
        if raw[i] != 0xFF:
            break
        while i < n and raw[i] == 0xFF:
            i += 1
        if i >= n:
            break
        marker = raw[i]
        i += 1
        if marker in (0xD8, 0xD9, 0x01) or (0xD0 <= marker <= 0xD7):
            continue
        if i + 2 > n:
            break
        seglen = struct.unpack(">H", raw[i : i + 2])[0]
        seg = raw[i + 2 : i + seglen]
        if marker in (0xC0, 0xC1, 0xC2) and len(seg) >= 5:
            info["height"], info["width"] = struct.unpack(">HH", seg[1:5])
        elif marker == 0xE1 and seg.startswith(b"Exif\x00\x00"):
            ascii_hits = re.findall(rb"[ -~]{4,64}", seg)
            info["exif_strings"] = [s.decode("ascii", "ignore") for s in ascii_hits[:12]]
        elif marker == 0xE1 and seg.startswith(b"http://ns.adobe.com/xap/1.0/\x00"):
            xmp = seg[29:].decode("utf-8", "ignore")
            info["xmp_xml"] += xmp
            if "hdrgm" in xmp or "GainMap" in xmp:
                info["has_gainmap"] = True
            if "MotionPhoto" in xmp or "MicroVideo" in xmp:
                info["has_motion_photo"] = True
        elif marker == 0xE2 and seg.startswith(b"MPF\x00"):
            info["has_mpf"] = True
        i += seglen
        if marker == 0xDA:
            break
    # Locate embedded MP4 ('ftyp' box at +4 bytes) for MotionPhoto / LivePhoto verification
    ftyp = raw.find(b"ftyp")
    if ftyp >= 4:
        box_sz = struct.unpack(">I", raw[ftyp - 4 : ftyp])[0]
        if 16 <= box_sz <= 128 and raw[ftyp + 4 : ftyp + 8] in (b"mp42", b"isom", b"mp41", b"qt  ", b"M4V ", b"heic", b"mif1"):
            if ftyp - 4 > 0:
                info["mp4_offset"] = ftyp - 4
                info["mp4_size"] = len(raw) - (ftyp - 4)
    return info


def _decode_rgb_grid(jpg_path: Path, grid: int = 4):
    """Decode JPEG downscaled and compute per-row/per-cell RGB + green-corruption stats."""
    try:
        from PIL import Image
        im = Image.open(jpg_path).convert("RGB")
        w, h = im.size
        small = im.resize((256, 256))
        px = small.load()
        rows = []
        for gy in range(grid):
            y0, y1 = gy * (256 // grid), (gy + 1) * (256 // grid)
            tot = green_cnt = black_cnt = 0
            sr = sg = sb = 0
            for y in range(y0, y1):
                for x in range(256):
                    r, g, b = px[x, y]
                    sr += r; sg += g; sb += b
                    tot += 1
                    if g > r + 35 and g > b + 35:
                        green_cnt += 1
                    if r < 6 and g < 6 and b < 6:
                        black_cnt += 1
            rows.append((sr / tot, sg / tot, sb / tot, 100.0 * green_cnt / tot, 100.0 * black_cnt / tot))
        return (w, h), rows
    except Exception:
        pass
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None, None
    res = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(jpg_path), "-vf", "scale=256:256", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True,
    )
    if res.returncode != 0 or len(res.stdout) < 256 * 256 * 3:
        return None, None
    buf = res.stdout
    rows = []
    for gy in range(grid):
        y0, y1 = gy * (256 // grid), (gy + 1) * (256 // grid)
        tot = green_cnt = black_cnt = 0
        sr = sg = sb = 0
        for y in range(y0, y1):
            base = y * 256 * 3
            for x in range(256):
                off = base + x * 3
                r, g, b = buf[off], buf[off + 1], buf[off + 2]
                sr += r; sg += g; sb += b
                tot += 1
                if g > r + 35 and g > b + 35:
                    green_cnt += 1
                if r < 6 and g < 6 and b < 6:
                    black_cnt += 1
        rows.append((sr / tot, sg / tot, sb / tot, 100.0 * green_cnt / tot, 100.0 * black_cnt / tot))
    return None, rows


def cmd_inspect_jpg(args):
    """Safe Python-only JPEG/50MP inspector (never passes raw image to LLM API)."""
    for fpath in args.files:
        p = Path(fpath)
        if not p.exists():
            print(f"[MISSING] {p}")
            continue
        raw = p.read_bytes()
        info = _parse_jpeg_header_info(raw)
        dims_dec, rows = _decode_rgb_grid(p, grid=4)
        w = info["width"] or (dims_dec[0] if dims_dec else "?")
        h = info["height"] or (dims_dec[1] if dims_dec else "?")
        print(f"=== {p.name} ({info['size_bytes']:,} bytes = {info['size_bytes'] / (1024*1024):.2f} MB) ===")
        print(f"  Dims={w}x{h}  SOI={info['valid_soi']}  EOI={info['valid_eoi']}  MPF={info['has_mpf']}  GainMap={info['has_gainmap']}  MotionPhoto={info['has_motion_photo']}")
        if info["exif_strings"]:
            print(f"  EXIF={info['exif_strings'][:8]}")
        if info["xmp_xml"]:
            tags = re.findall(r'(?:GCamera|Camera|Container|Item|hdrgm):[A-Za-z0-9_]+="[^"]*"', info["xmp_xml"])
            if tags:
                print(f"  XMP={tags[:14]}")
        if info["mp4_offset"] is not None:
            print(f"  Embedded MP4: offset={info['mp4_offset']:,}  size={info['mp4_size']:,} bytes ({info['mp4_size']/1024:.1f} KB)")
            ffprobe = shutil.which("ffprobe")
            if ffprobe:
                with tempfile.NamedTemporaryFile(suffix=".mp4") as tf:
                    tf.write(raw[info["mp4_offset"] :])
                    tf.flush()
                    pr = subprocess.run(
                        [ffprobe, "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,nb_frames,duration", "-of", "compact=p=0", tf.name],
                        capture_output=True, text=True,
                    )
                    for sl in pr.stdout.strip().splitlines():
                        print(f"    MP4 stream: {sl}")
        bad_bands = 0
        if rows:
            for idx, (r, g, b, gp, bp) in enumerate(rows):
                flag = "  <-- CORRUPTED!" if gp > 25.0 or (bp > 95.0 and idx >= 2) else ""
                if flag:
                    bad_bands += 1
                print(f"  Band {idx+1}/4: avgRGB=({r:5.1f},{g:5.1f},{b:5.1f})  green={gp:5.1f}%  black={bp:5.1f}%{flag}")
        valid_container = info["valid_soi"] and (info["valid_eoi"] or info["mp4_offset"] is not None)
        verdict = "PASS (FULL FRAME)" if (valid_container and bad_bands == 0 and rows) else "FAIL / TRUNCATED"
        print(f"  VERDICT: {verdict}\n")


def compute_dex_stats(smali_dir: Path) -> dict:
    """Compute accurate DEX index table counts for a smali* directory."""
    methods = set()
    fields = set()
    types = set()
    const_strings = set()
    file_count = 0

    cls_re = re.compile(r"^\.class[^\n]+(L[^;\s]+;)", re.M)
    mdef_re = re.compile(r"^\.method[^\n]*? (\S+\([^)]*\)\S+)\s*$", re.M)
    fdef_re = re.compile(r"^\.field[^\n]*? (\S+:\S+)", re.M)
    mref_re = re.compile(r"(L[^;\s]+;->\S+\([^)]*\)\S+)")
    fref_re = re.compile(r"(L[^;\s]+;->[^\s(:]+:\S+)")
    type_re = re.compile(r"(L[^;\s]+;)")
    cstr_re = re.compile(r'const-string(?:/jumbo)?\s+[pv]\d+,\s*(".*")')

    for f in smali_dir.rglob("*.smali"):
        file_count += 1
        t = f.read_text(encoding="utf-8", errors="ignore")
        mc = cls_re.search(t)
        cur_cls = mc.group(1) if mc else ""
        if cur_cls:
            types.add(cur_cls)
            for md in mdef_re.findall(t):
                methods.add(f"{cur_cls}->{md}")
            for fd in fdef_re.findall(t):
                fields.add(f"{cur_cls}->{fd}")
        methods.update(mref_re.findall(t))
        fields.update(fref_re.findall(t))
        types.update(type_re.findall(t))
        const_strings.update(cstr_re.findall(t))

    return {
        "dir": smali_dir.name,
        "files": file_count,
        "methods": len(methods),
        "fields": len(fields),
        "types": len(types),
        "const_strings": len(const_strings),
    }


def cmd_dex_stats(ws: pc.SmaliWorkspace, _args):
    print(f"{'DEX dir':16s} {'Files':>6s} {'Methods':>8s} {'Fields':>8s} {'Types':>8s} {'Strings':>8s}  Status")
    print("-" * 72)
    for d in sorted(ws.root.glob("smali*")):
        if not d.is_dir():
            continue
        st = compute_dex_stats(d)
        peak = max(st["methods"], st["fields"], st["types"])
        flag = "OVER 65535!" if peak > 65535 else ("NEAR LIMIT" if peak > 61000 else "OK")
        print(
            f"{st['dir']:16s} {st['files']:6d} {st['methods']:8d} {st['fields']:8d} "
            f"{st['types']:8d} {st['const_strings']:8d}  {flag}"
        )


def cmd_check_profile(ws: pc.SmaliWorkspace, args):
    profile = json.loads(args.config.read_text(encoding="utf-8"))
    _, common_to_jacoco, _, _ = build_reverse_maps(ws)
    print(f"Profile: {profile.get('profile_name')}  (against {ws.root})\n")
    print(f"{'Feature Key':32s} {'Enabled':7s} {'Resolved Sig':16s} {'Onyx->New':14s} {'Jacoco Keys'}")
    print("-" * 98)
    ok_cnt = 0
    fail_cnt = 0
    for fkey, spec in profile.get("onyx_feature_overrides", {}).items():
        en = spec.get("enabled", True)
        sig = pc.resolve_feature_method(ws, fkey)
        if sig:
            ok_cnt += 1
            _, cur = eval_simple_value(ws, "com.mi.device.Onyx", sig)
            jks = ",".join(sorted(common_to_jacoco.get(sig, ()))[:2])
            print(f"{fkey:32s} {str(en):7s} {sig:16s} {cur[:6]}->{str(spec['value']):<6s} {jks}")
        else:
            fail_cnt += 1
            print(f"{fkey:32s} {str(en):7s} {'UNRESOLVED':16s} {'-':14s}")
    print(f"\nResolved: {ok_cnt}/{ok_cnt + fail_cnt} (failed: {fail_cnt})")


def main():
    ap = argparse.ArgumentParser(description="Obfuscation-Resilient MiuiCamera Smali Inspector")
    ap.add_argument("-d", "--dir", type=Path, default=DEFAULT_DEC, help="Decompiled APK directory")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("summary", help="Show discovered core classes")

    p_dev = sub.add_parser("devices", help="List XOR-decrypted com.mi.device.* classes")
    p_dev.add_argument("--filter", default=None)

    p_diff = sub.add_parser("diff", help="Diff capability methods between two devices")
    p_diff.add_argument("dev_a")
    p_diff.add_argument("dev_b")
    p_diff.add_argument("--filter", default=None)

    p_find = sub.add_parser("find", help="Search string/anchor across smali with auto-annotations")
    p_find.add_argument("needle")
    p_find.add_argument("--all", action="store_true", help="Include obfuscated device classes")
    p_find.add_argument("--context", type=int, default=0, help="Lines of annotated context around match")
    p_find.add_argument("--limit", type=int, default=30)

    p_xref = sub.add_parser("xref", help="Find callers/references to class or class->method")
    p_xref.add_argument("target")
    p_xref.add_argument("--context", type=int, default=0, help="Lines of annotated context around match")
    p_xref.add_argument("--limit", type=int, default=30)

    p_dump = sub.add_parser("dump", help="Dump class, method, or line range with inline deobfuscation comments")
    p_dump.add_argument("target")
    p_dump.add_argument("method", nargs="?", default=None)
    p_dump.add_argument("--lines", default=None, help="Line range START:END (1-indexed)")
    p_dump.add_argument("--max-lines", type=int, default=200)

    p_res = sub.add_parser("res", help="Search strings.xml/public.xml/R$string by keyword or hex ID")
    p_res.add_argument("query")
    p_res.add_argument("--callers", action="store_true", help="Also list smali files referencing matched IDs")
    p_res.add_argument("--limit", type=int, default=40)

    p_prp = sub.add_parser("props", help="Scan system properties across smali")
    p_prp.add_argument("filter", nargs="?", default=None)
    p_prp.add_argument("--limit", type=int, default=60)

    p_jac = sub.add_parser("jacoco", help="Search JacocoIgnore keys with Onyx vs Nezha values")
    p_jac.add_argument("filter", nargs="?", default=None)
    p_jac.add_argument("--limit", type=int, default=60)

    p_vt = sub.add_parser("vtags", help="Search Camera2 vendor tag strings")
    p_vt.add_argument("filter", nargs="?", default=None)
    p_vt.add_argument("--limit", type=int, default=60)

    p_fw = sub.add_parser("field-writers", help="Find every sput/iput writing to a field")
    p_fw.add_argument("target", help="Class->field (e.g. Vr/i->f or Preference->e)")
    p_fw.add_argument("--context", type=int, default=4)
    p_fw.add_argument("--limit", type=int, default=20)

    p_pr = sub.add_parser("prefs", help="Summarize preference keys, fields, and methods in a Fragment/class")
    p_pr.add_argument("target")

    p_jpg = sub.add_parser("inspect-jpg", help="Safe Python-only JPEG/50MP/MotionPhoto/UltraHDR container inspector")
    p_jpg.add_argument("files", nargs="+")

    sub.add_parser("dex-stats", help="Compute DEX 64K table pressure for every smali* dir")

    p_chk = sub.add_parser("check-profile", help="Dry-run feature resolvers from port_profile.json")
    p_chk.add_argument("-c", "--config", type=Path, default=DEFAULT_PROFILE)

    args = ap.parse_args()
    if args.cmd == "inspect-jpg":
        cmd_inspect_jpg(args)
        return
    if args.cmd == "res":
        cmd_res(args.dir, args)
        return

    ws = load_workspace(args.dir)
    {
        "summary": cmd_summary,
        "devices": cmd_devices,
        "diff": cmd_diff,
        "find": cmd_find,
        "xref": cmd_xref,
        "dump": cmd_dump,
        "props": cmd_props,
        "jacoco": cmd_jacoco,
        "vtags": cmd_vtags,
        "field-writers": cmd_field_writers,
        "prefs": cmd_prefs,
        "dex-stats": cmd_dex_stats,
        "check-profile": cmd_check_profile,
    }[args.cmd](ws, args)


if __name__ == "__main__":
    main()
