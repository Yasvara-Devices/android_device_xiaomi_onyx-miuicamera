#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Automated MiuiCamera Feature Matrix & Obfuscation Analyzer.
Works across any MiuiCamera 6.x+ update by dynamically discovering:
  - XOR-obfuscated com.mi.device.* classes (Common, Onyx, Nezha, etc.)
  - DataItemFeature wrapper class (holder of Common instance)
  - Full method-by-method diff between Onyx and Nezha (and Leitz variants)
  - Human-readable context hints from every caller across the APK
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path


def decode_cls_name(obf: str) -> str:
    s = obf.strip("L;")
    if not s or ord(s[0]) < 128:
        return s
    key = ord(s[0]) ^ ord("c")
    dec = "".join(chr(ord(ch) ^ key) for ch in s)
    if dec.startswith("com."):
        return dec
    return f"?({obf})"


def parse_methods(smali_text: str):
    """Return dict: (name, sig) -> body_text"""
    methods = {}
    for m in re.finditer(
        r"^\.method[^\n]*? (\S+?)\(([^)]*)\)(\S+)\n(.*?)^\.end method",
        smali_text,
        re.M | re.S,
    ):
        name, args, ret, body = m.group(1), m.group(2), m.group(3), m.group(4)
        methods[(name, f"({args}){ret}")] = body
    return methods


def eval_simple_body(body: str, device_chain_obf: set, all_classes_obf_to_dec: dict):
    """Evaluate simple getter methods (boolean/int/float/String/instance-of)."""
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith((".", "#", ":"))]
    # Remove register annotations / line / local directives already skipped by "."
    if not lines:
        return ("empty", None)
    # Single const return
    if len(lines) == 2 and lines[1].startswith("return"):
        op = lines[0]
        m = re.match(r"const(?:/4|/16|/high16)?\s+[pv]\d+,\s*(-?0x[0-9a-fA-F]+|-?\d+)", op)
        if m:
            val = int(m.group(1), 0)
            return ("const", val)
        m = re.match(r'const-string(?:/jumbo)?\s+[pv]\d+,\s*"(.*)"', op)
        if m:
            return ("string", m.group(1))
        m = re.match(r"instance-of\s+[pv]\d+,\s*p0,\s*(L[^;\s]+;)", op)
        if m:
            target = m.group(1)
            return ("instanceof", (target in device_chain_obf, all_classes_obf_to_dec.get(target, target)))
    # Multi-branch instance-of (OR / AND chain of instanceof checks)
    insts = re.findall(r"instance-of\s+[pv]\d+,\s*p0,\s*(L[^;\s]+;)", body)
    if insts and "invoke-" not in body and "sget" not in body and "iget" not in body:
        # Check if simple OR returning 1 if any match, or negated returning 0 if any match
        dec_targets = [all_classes_obf_to_dec.get(t, t) for t in insts]
        matched = [all_classes_obf_to_dec.get(t, t) for t in insts if t in device_chain_obf]
        # Detect polarity: if first branch after instance-of is `if-eqz` -> returns 1 on match or 0 on match
        # Simulate basic block roughly for pure instance-of boolean chains
        val = simulate_instanceof_bool(body, device_chain_obf)
        return ("instanceof_multi", (val, matched, dec_targets))
    # String decryption call LHh/c;->o(I, String) or similar
    m_dec = re.search(r'const-string(?:/jumbo)?\s+[pv]\d+,\s*"([^"]*)"', body)
    if m_dec and "return-object" in body and len(lines) <= 8:
        return ("expr_str", m_dec.group(1)[:60])
    # Simple super call or complex
    compact = " ; ".join(lines[:6])
    return ("complex", compact[:140])


def simulate_instanceof_bool(body: str, device_chain_obf: set):
    """Exact bytecode interpreter for pure instance-of / branch / const boolean methods."""
    raw_lines = [l.strip() for l in body.splitlines() if l.strip()]
    labels = {}
    insns = []
    for l in raw_lines:
        if l.startswith(":"):
            labels[l] = len(insns)
        elif not l.startswith((".", "#")):
            insns.append(l)
    regs = {"p0": "THIS"}
    pc = 0
    steps = 0
    while 0 <= pc < len(insns) and steps < 200:
        steps += 1
        ins = insns[pc]
        m = re.match(r"instance-of\s+(\w+),\s*p0,\s*(L[^;\s]+;)", ins)
        if m:
            regs[m.group(1)] = 1 if m.group(2) in device_chain_obf else 0
            pc += 1
            continue
        m = re.match(r"const(?:/4|/16)?\s+(\w+),\s*(-?0x[0-9a-fA-F]+|-?\d+)", ins)
        if m:
            regs[m.group(1)] = int(m.group(2), 0)
            pc += 1
            continue
        m = re.match(r"if-eqz\s+(\w+),\s*(:\S+)", ins)
        if m:
            if regs.get(m.group(1), 0) == 0:
                pc = labels[m.group(2)]
            else:
                pc += 1
            continue
        m = re.match(r"if-nez\s+(\w+),\s*(:\S+)", ins)
        if m:
            if regs.get(m.group(1), 0) != 0:
                pc = labels[m.group(2)]
            else:
                pc += 1
            continue
        m = re.match(r"goto(?:/16|/32)?\s+(:\S+)", ins)
        if m:
            pc = labels[m.group(1)]
            continue
        m = re.match(r"return\s+(\w+)", ins)
        if m:
            return bool(regs.get(m.group(1), 0))
        m = re.match(r"xor-int/lit8\s+(\w+),\s*(\w+),\s*(0x[0-9a-fA-F]+|\d+)", ins)
        if m:
            regs[m.group(1)] = regs.get(m.group(2), 0) ^ int(m.group(3), 0)
            pc += 1
            continue
        m = re.match(r"move\s+(\w+),\s*(\w+)", ins)
        if m:
            regs[m.group(1)] = regs.get(m.group(2), 0)
            pc += 1
            continue
        return None
    return None


def main():
    root = Path(sys.argv[1])
    out_json = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    # 1. Load all obfuscated com.mi.device.* classes
    obf_to_dec = {}
    dec_to_obf = {}
    cls_super = {}
    cls_file = {}
    cls_methods = {}

    for p in root.rglob("*.smali"):
        if not any(ord(c) > 127 for c in p.name):
            continue
        txt = p.read_text(errors="replace")
        mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", txt, re.M)
        ms = re.search(r"^\.super (L[^;\s]+;)", txt, re.M)
        if not mc:
            continue
        obf = mc.group(1)
        dec = decode_cls_name(obf)
        obf_to_dec[obf] = dec
        dec_to_obf[dec] = obf
        cls_super[obf] = ms.group(1) if ms else ""
        cls_file[obf] = p
        cls_methods[obf] = parse_methods(txt)

    common_obf = dec_to_obf.get("com.mi.device.common.Common")
    onyx_obf = dec_to_obf.get("com.mi.device.Onyx")
    nezha_obf = dec_to_obf.get("com.mi.device.Nezha")
    print(f"Common: {common_obf} ({cls_file[common_obf].relative_to(root)})")
    print(f"Onyx  : {onyx_obf} ({cls_file[onyx_obf].relative_to(root)})")
    print(f"Nezha : {nezha_obf} ({cls_file[nezha_obf].relative_to(root)})")

    # Print all Nezha / Onyx / Leitz related classes
    for d in sorted(dec_to_obf):
        if any(k in d.lower() for k in ("nezha", "onyx", "leitz", "leica", "xuanyuan", "popsicle", "pandora", "pudding", "commonflagship", "commonnote")):
            sup = obf_to_dec.get(cls_super[dec_to_obf[d]], cls_super[dec_to_obf[d]])
            print(f"  [class] {d:42s} extends {sup}")

    def get_chain(obf):
        chain = []
        cur = obf
        while cur and cur in cls_super:
            chain.append(cur)
            cur = cls_super[cur]
        return chain

    onyx_chain = get_chain(onyx_obf)
    nezha_chain = get_chain(nezha_obf)
    print("Onyx chain :", [obf_to_dec.get(c, c) for c in onyx_chain])
    print("Nezha chain:", [obf_to_dec.get(c, c) for c in nezha_chain])

    # 2. Find DataItemFeature wrapper class (references common_obf as an instance field)
    wrapper_file = None
    wrapper_cls = None
    wrapper_txt = ""
    for p in root.rglob("*.smali"):
        if any(ord(c) > 127 for c in p.name):
            continue
        # Quick size filter: DataItemFeature is huge (> 50 KB)
        if p.stat().st_size < 40000:
            continue
        txt = p.read_text(errors="replace")
        if common_obf in txt and txt.count(f"{common_obf}->") > 100:
            wrapper_file = p
            mc = re.search(r"^\.class[^\n]+(L[^;\s]+;)", txt, re.M)
            wrapper_cls = mc.group(1) if mc else None
            wrapper_txt = txt
            break

    print(f"DataItemFeature wrapper: {wrapper_cls} ({wrapper_file.relative_to(root) if wrapper_file else 'NOT FOUND'})")

    # Map Common method -> Wrapper method(s)
    common_to_wrapper = defaultdict(list)
    wrapper_methods = parse_methods(wrapper_txt) if wrapper_txt else {}
    for (wname, wsig), wbody in wrapper_methods.items():
        for cm, csig in re.findall(re.escape(common_obf) + r"->(\w+)(\([^)]*\)\S+)", wbody):
            common_to_wrapper[(cm, csig)].append((wname, wsig))

    # 3. Resolve every method on Onyx vs Nezha
    all_keys = set()
    for c in set(onyx_chain) | set(nezha_chain):
        for k in cls_methods.get(c, {}):
            if k[0] not in ("<init>", "<clinit>"):
                all_keys.add(k)

    def resolve(chain, key):
        for c in chain:
            if key in cls_methods.get(c, {}):
                body = cls_methods[c][key]
                ev = eval_simple_body(body, set(chain), obf_to_dec)
                return obf_to_dec.get(c, c), ev, body
        return None, ("missing", None), ""

    diffs = []
    wrapper_targets = set()
    for key in sorted(all_keys):
        o_cls, o_ev, o_body = resolve(onyx_chain, key)
        n_cls, n_ev, n_body = resolve(nezha_chain, key)
        # Normalize boolean values for comparison
        def norm(ev):
            kind, val = ev
            if kind == "const":
                return val
            if kind == "instanceof":
                return 1 if val[0] else 0
            if kind == "instanceof_multi":
                return 1 if val[0] else 0
            if kind == "string":
                return val
            return ev
        if norm(o_ev) != norm(n_ev):
            wm = common_to_wrapper.get(key, [])
            for w in wm:
                wrapper_targets.add(f"{wrapper_cls}->{w[0]}{w[1]}")
            diffs.append({
                "common_method": f"{key[0]}{key[1]}",
                "wrapper_methods": [f"{w[0]}{w[1]}" for w in wm],
                "onyx_defined_in": o_cls,
                "onyx_val": repr(norm(o_ev)),
                "onyx_raw": repr(o_ev),
                "nezha_defined_in": n_cls,
                "nezha_val": repr(norm(n_ev)),
                "nezha_raw": repr(n_ev),
            })

    print(f"\nTotal Common/hierarchy methods: {len(all_keys)}")
    print(f"Methods differing between Onyx and Nezha: {len(diffs)}")

    # 4. Scan all smali files ONCE to gather caller context strings for each differing wrapper method!
    callers = defaultdict(list)
    if wrapper_cls and wrapper_targets:
        pat = re.compile(re.escape(wrapper_cls) + r"->(\w+\([^)]*\)\S+)")
        str_pat = re.compile(r'const-string(?:/jumbo)?\s+[pv]\d+,\s*"([^"]{3,80})"')
        for p in root.rglob("*.smali"):
            if p == wrapper_file or any(ord(c) > 127 for c in p.name):
                continue
            txt = p.read_text(errors="replace")
            if wrapper_cls not in txt:
                continue
            lines = txt.splitlines()
            rel = str(p.relative_to(root))
            for i, line in enumerate(lines):
                if wrapper_cls not in line:
                    continue
                for m in pat.finditer(line):
                    full = f"{wrapper_cls}->{m.group(1)}"
                    if full in wrapper_targets and len(callers[full]) < 4:
                        # Grab enclosing method + nearby string constants
                        m_hdr = ""
                        for j in range(i, max(-1, i - 120), -1):
                            if lines[j].startswith(".method "):
                                m_hdr = lines[j].strip()
                                break
                        nearby = "\n".join(lines[max(0, i - 15) : min(len(lines), i + 15)])
                        strs = [s for s in str_pat.findall(nearby) if not s.startswith("\\u")]
                        callers[full].append({"file": rel, "method": m_hdr, "strings": strs[:6]})

    # Attach caller context
    for d in diffs:
        ctx = []
        for wm in d["wrapper_methods"]:
            full = f"{wrapper_cls}->{wm}"
            if full in callers:
                ctx.extend(callers[full])
        d["callers"] = ctx[:5]

    if out_json:
        out_json.write_text(json.dumps({
            "common_obf": common_obf,
            "onyx_obf": onyx_obf,
            "nezha_obf": nezha_obf,
            "wrapper_cls": wrapper_cls,
            "wrapper_file": str(wrapper_file.relative_to(root)) if wrapper_file else None,
            "total_methods": len(all_keys),
            "diff_count": len(diffs),
            "diffs": diffs,
        }, indent=2, ensure_ascii=False))
        print(f"Saved full feature diff matrix to {out_json}")

    # Print a compact summary of boolean diffs where Nezha=True and Onyx=False
    bool_enable = [d for d in diffs if d["common_method"].endswith("()Z") and d["onyx_val"] == "0" and d["nezha_val"] == "1"]
    print(f"\n=== BOOLEAN CAPABILITIES: Nezha=TRUE, Onyx=FALSE ({len(bool_enable)}) ===")
    for d in bool_enable:
        hints = []
        for c in d["callers"]:
            fshort = Path(c["file"]).stem
            s = ", ".join(c["strings"][:3])
            hints.append(f"{fshort}:[{s}]" if s else fshort)
        print(f"  {d['common_method']:12s} wrap={','.join(d['wrapper_methods'][:2]):18s} | {d['nezha_defined_in'].split('.')[-1]:15s} | {' ; '.join(hints[:2])}")


if __name__ == "__main__":
    main()
