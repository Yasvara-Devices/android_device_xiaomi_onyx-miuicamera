#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Reusable Xiaomi HyperOS OTA Camera Asset Extractor.
Reads payload.bin directly inside a full-OTA .zip (zero-copy for ZIP_STORED)
or a standalone payload.bin, unpacks target partitions one-by-one to save disk
space, inspects EROFS/ext4 layouts, and extracts all MiuiCamera APKs, JNI libs,
Leica/Leitzphone watermarks, LUTs, shutter audio, device features, and props.
"""

import argparse
import bz2
import concurrent.futures
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def _varint(buf: bytes, pos: int):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7


def _fields(buf: bytes):
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        fnum, wtype = key >> 3, key & 7
        if wtype == 0:
            val, pos = _varint(buf, pos)
        elif wtype == 1:
            val = buf[pos : pos + 8]
            pos += 8
        elif wtype == 2:
            ln, pos = _varint(buf, pos)
            val = buf[pos : pos + ln]
            pos += ln
        elif wtype == 5:
            val = buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wtype}")
        yield fnum, val


def _extents(buf: bytes):
    start = num = 0
    for f, v in _fields(buf):
        if f == 1:
            start = v
        elif f == 2:
            num = v
    return start, num


def open_payload_source(path: Path):
    """Return (file_obj, base_offset, close_fn) pointing to CrAU payload.bin."""
    if zipfile.is_zipfile(path):
        zf = zipfile.ZipFile(path)
        info = zf.getinfo("payload.bin")
        zf.close()
        if info.compress_type != zipfile.ZIP_STORED:
            raise RuntimeError("payload.bin inside zip is compressed; please unzip payload.bin first")
        fp = open(path, "rb")
        fp.seek(info.header_offset)
        lh = fp.read(30)
        assert lh[:4] == b"PK\x03\x04", "Bad zip local header"
        fn_len, ex_len = struct.unpack("<HH", lh[26:30])
        base_offset = info.header_offset + 30 + fn_len + ex_len
        return fp, base_offset
    return open(path, "rb"), 0


def parse_manifest(fp, base_offset: int):
    fp.seek(base_offset)
    hdr = fp.read(24)
    if hdr[:4] != b"CrAU":
        raise RuntimeError(f"Invalid payload magic: {hdr[:4]!r}")
    ver, msize = struct.unpack(">QQ", hdr[4:20])
    sigsize = struct.unpack(">I", hdr[20:24])[0] if ver >= 2 else 0
    if ver < 2:
        fp.seek(base_offset + 20)
    manifest = fp.read(msize)
    data_start = fp.tell() + sigsize
    block_size = 4096
    parts = {}
    for f, v in _fields(manifest):
        if f == 3:
            block_size = v
        elif f == 13:
            name = None
            size = 0
            ops = []
            for pf, pv in _fields(v):
                if pf == 1:
                    name = pv.decode()
                elif pf == 7:
                    for sf, sv in _fields(pv):
                        if sf == 1:
                            size = sv
                elif pf == 8:
                    ops.append(pv)
            if name:
                parts[name] = {"size": size, "ops": ops}
    return block_size, data_start, parts


def _decompress_op(otype: int, blob: bytes) -> bytes:
    if otype == 0:
        return blob
    if otype == 1:
        return bz2.decompress(blob)
    if otype == 8:
        return lzma.decompress(blob)
    if otype == 14:
        return subprocess.run(["zstd", "-dc"], input=blob, capture_output=True, check=True).stdout
    raise RuntimeError(f"Unsupported payload op type {otype}")


def extract_partition_img(source_path: Path, part_name: str, out_img: Path, workers: int = 8):
    fp, base_offset = open_payload_source(source_path)
    try:
        block_size, data_start, parts = parse_manifest(fp, base_offset)
        if part_name not in parts:
            raise KeyError(f"Partition '{part_name}' not found. Available: {sorted(parts.keys())}")
        pinfo = parts[part_name]
        ops_parsed = []
        for op in pinfo["ops"]:
            otype, off, ln, dst = 0, 0, 0, []
            for of, ov in _fields(op):
                if of == 1:
                    otype = ov
                elif of == 2:
                    off = ov
                elif of == 3:
                    ln = ov
                elif of == 6:
                    dst.append(_extents(ov))
            ops_parsed.append((otype, off, ln, dst))

        print(f"[extract_ota] Unpacking {part_name} ({pinfo['size']/1024/1024:.1f} MB, {len(ops_parsed)} ops)...", flush=True)
        with open(out_img, "wb") as ofp:
            if pinfo["size"]:
                ofp.truncate(pinfo["size"])
            # Process in chunks to keep RAM bounded while parallelizing decompression
            chunk_size = workers * 4
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
                for i in range(0, len(ops_parsed), chunk_size):
                    batch = ops_parsed[i : i + chunk_size]
                    futs = []
                    for otype, off, ln, dst in batch:
                        if otype == 6:  # ZERO
                            futs.append((None, dst, True))
                            continue
                        fp.seek(data_start + off)
                        blob = fp.read(ln)
                        futs.append((ex.submit(_decompress_op, otype, blob), dst, False))
                    for fut, dst, is_zero in futs:
                        if is_zero:
                            for s, n in dst:
                                ofp.seek(s * block_size)
                                ofp.write(b"\0" * (n * block_size))
                        else:
                            data = fut.result()
                            pos = 0
                            for s, n in dst:
                                ofp.seek(s * block_size)
                                ofp.write(data[pos : pos + n * block_size])
                                pos += n * block_size
    finally:
        fp.close()


def detect_fs(img_path: Path) -> str:
    with open(img_path, "rb") as f:
        f.seek(1024)
        sb_erofs = f.read(4)
        if sb_erofs == b"\xe2\xe1\xf5\xe0":
            return "erofs"
        f.seek(1024 + 0x38)
        sb_ext = f.read(2)
        if sb_ext == b"\x53\xef":
            return "ext4"
    return "unknown"


def erofs_ls(img_path: Path, vpath: str = "/"):
    res = subprocess.run(
        ["dump.erofs", "--ls", f"--path={vpath}", str(img_path)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return []
    entries = []
    # dump.erofs --ls lines look like: "   12345     2  filename" (nid, type, name)
    for line in res.stdout.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(.+?)\s*$", line)
        if m:
            nid, ftype, name = int(m.group(1)), int(m.group(2)), m.group(3)
            if name in (".", ".."):
                continue
            entries.append((nid, ftype, name))
    return entries


def erofs_walk(img_path: Path, root: str = "/", max_depth: int = 5):
    """Yield (full_path, ftype) where ftype 2=dir, 1=reg, 7=symlink."""
    queue = [(root.rstrip("/") or "/", 0)]
    while queue:
        cur, depth = queue.pop(0)
        for _nid, ftype, name in erofs_ls(img_path, cur):
            child = f"{cur.rstrip('/')}/{name}"
            yield child, ftype
            if ftype == 2 and depth < max_depth:
                queue.append((child, depth + 1))


def erofs_extract_path(img_path: Path, vpath: str, out_dir: Path) -> bool:
    """Extract a single file or directory from EROFS image to out_dir/<vpath>."""
    rel = vpath.lstrip("/")
    if not rel:
        return False
    res_info = subprocess.run(
        ["dump.erofs", f"--path={vpath}", str(img_path)],
        capture_output=True,
        text=True,
    )
    if res_info.returncode != 0:
        return False
    info = res_info.stdout
    # Check octal mode (04xxxx = dir, 12xxxx = symlink, 10xxxx = regular file)
    is_dir = bool(re.search(r"File type:\s*(directory|2)\b", info, re.I)) or bool(
        re.search(r"File mode:\s*0?4[0-7]{4}\b", info, re.I)
    ) or bool(re.search(r"Mode:\s*0?4[0-7]{4}\b", info, re.I)) or bool(
        re.search(r"\bd[rwx-]{9}\b", info)
    )
    is_link = bool(re.search(r"File type:\s*(symlink|7)\b", info, re.I)) or bool(
        re.search(r"(File )?Mode:\s*0?12[0-7]{4}\b", info, re.I)
    ) or bool(re.search(r"\bl[rwx-]{9}\b", info))
    if not is_dir and not is_link:
        # Fallback: try dump.erofs --ls; succeeds only on directories
        ls_try = subprocess.run(
            ["dump.erofs", "--ls", f"--path={vpath}", str(img_path)],
            capture_output=True,
            text=True,
        )
        if ls_try.returncode == 0 and re.search(r"^\s*\d+\s+2\s+\.\s*$", ls_try.stdout, re.M):
            is_dir = True

    dest = out_dir / rel
    if is_dir:
        dest.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ["fsck.erofs", "--overwrite", "--no-preserve", f"--path={vpath}", f"--extract={dest}", str(img_path)],
            capture_output=True,
            text=True,
        )
        return r.returncode == 0
    if is_link:
        # Skip raw symlinks or record target if needed
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_dir():
        return False
    r = subprocess.run(
        ["dump.erofs", "--cat", f"--path={vpath}", str(img_path)],
        capture_output=True,
    )
    if r.returncode != 0:
        return False
    dest.write_bytes(r.stdout)
    return True


def dump_partition_camera_assets(part: str, img_path: Path, out_root: Path):
    fs = detect_fs(img_path)
    print(f"[extract_ota] Inspecting {part} (fs={fs})...", flush=True)
    part_out = out_root / part
    part_out.mkdir(parents=True, exist_ok=True)

    if fs != "erofs":
        print(f"[extract_ota] WARNING: {part} is fs={fs}, not erofs — skipping auto-walk", flush=True)
        return {}

    # Always save a full inventory up to depth 4 for reference
    inv = []
    for p, ft in erofs_walk(img_path, "/", max_depth=4):
        inv.append(f"{ft}\t{p}")
    (part_out / "_inventory.txt").write_text("\n".join(inv) + "\n")

    # Define targeted paths and patterns per partition
    exact_paths = []
    regex_patterns = []

    if part == "product":
        exact_paths = [
            "/etc/build.prop",
            "/build.prop",
            "/priv-app/MiuiCamera",
            "/etc/device_features",
            "/etc/pipeline_infos/com.android.camera",
            "/media/audio/ui",
        ]
        regex_patterns = [
            r"^/(priv-app|app)/.*(Camera|Leica|Leitz|Watermark|Filter|MiuiGallery|MediaEditor).*$",
            r"^/etc/(permissions|sysconfig|default-permissions)/.*(camera|leica|miui|xiaomi).*$",
            r"^/overlay/.*(Camera|Leica|MiuiCamera).*$",
            r"^/media/theme/.*(leica|leitz|camera).*$",
        ]
    elif part == "system_ext":
        exact_paths = [
            "/etc/build.prop",
            "/build.prop",
        ]
        regex_patterns = [
            r"^/lib64/.*(camera|campostproc|mianode|algoup|miui|leica|minikin).*\.so$",
            r"^/etc/(public\.libraries|permissions|sysconfig).*$",
            r"^/framework/.*(miui|xiaomi|camera).*\.jar$",
            r"^/(priv-app|app)/.*(Camera|Leica|Leitz).*$",
        ]
    elif part == "system":
        exact_paths = [
            "/system/build.prop",
            "/system/etc/public.libraries.txt",
        ]
        regex_patterns = [
            r"^/system/etc/public\.libraries.*\.txt$",
            r"^/system/etc/(permissions|sysconfig)/.*(camera|xiaomi|miui).*$",
            r"^/system/lib64/.*(camera|campostproc|mianode|algoup|minikin\.xiaomi).*\.so$",
        ]
    elif part in ("mi_ext", "mi_product"):
        # Small partitions: extract key props, themes, media, overlays, etc.
        exact_paths = [
            "/etc/build.prop",
            "/build.prop",
            "/product",
            "/system",
            "/etc",
        ]
    elif part == "odm":
        exact_paths = [
            "/etc/build.prop",
            "/build.prop",
            "/etc/camera/watermark",
            "/etc/camera/xiaomi",
            "/etc/camera/cameracustomize.json",
            "/etc/camera/camerascene.json",
            "/etc/camera/cameraopt.json",
            "/etc/camera/camxoverridesettings.txt",
            "/etc/camera/beauty_config_cn.json",
            "/etc/camera/beauty_config_gl.json",
            "/etc/camera/Version.txt",
            "/etc/camera/HIS_LensConf.json",
        ]
        regex_patterns = [
            r"^/etc/camera/.*(leica|leitz|lut|filter|watermark|smooth|zoom|plugin|master|style|cine|portrait|film|beauty|mivi|config).*$",
            r"^/lib64/camera/plugins/.*(watermark|filter|leica|leitz|beauty|bokeh|portrait).*\.so$",
            r"^/lib64/.*(minikin|watermark|leica|leitz|micamera_hal_policy).*\.so$",
        ]
    elif part == "vendor":
        exact_paths = [
            "/build.prop",
            "/etc/build.prop",
            "/etc/camera",
        ]
        regex_patterns = [
            r"^/etc/camera/.*$",
        ]

    extracted = []
    seen = set()

    for vp in exact_paths:
        if vp in seen:
            continue
        if erofs_extract_path(img_path, vp, part_out):
            seen.add(vp)
            extracted.append(vp)
            print(f"  + {part}:{vp}", flush=True)

    compiled = [re.compile(pat, re.I) for pat in regex_patterns]
    if compiled:
        for line in inv:
            ft_s, vp = line.split("\t", 1)
            if vp in seen:
                continue
            # Skip if a parent directory was already extracted
            if any(vp.startswith(s.rstrip("/") + "/") for s in seen):
                continue
            if any(rx.search(vp) for rx in compiled):
                if erofs_extract_path(img_path, vp, part_out):
                    seen.add(vp)
                    extracted.append(vp)
                    print(f"  + {part}:{vp}", flush=True)

    return {"fs": fs, "extracted": extracted, "inventory_count": len(inv)}


def main():
    ap = argparse.ArgumentParser(description="Extract Xiaomi camera assets from HyperOS full-OTA zip or payload.bin")
    ap.add_argument("ota", type=Path, help="Path to OTA .zip or payload.bin")
    ap.add_argument("-o", "--out", type=Path, required=True, help="Output directory (e.g. source/nezha_OS4.0.7.0)")
    ap.add_argument(
        "-p",
        "--partitions",
        nargs="+",
        default=["mi_product", "mi_ext", "system_ext", "system", "vendor", "product", "odm"],
        help="Partitions to unpack and inspect",
    )
    ap.add_argument("-j", "--jobs", type=int, default=8, help="Parallel decompression threads")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    summary = {"ota": str(args.ota), "partitions": {}}

    with tempfile.TemporaryDirectory(prefix="nezha_ota_", dir=str(args.out.parent)) as tmp:
        tmp_path = Path(tmp)
        for part in args.partitions:
            img = tmp_path / f"{part}.img"
            try:
                extract_partition_img(args.ota, part, img, workers=args.jobs)
                summary["partitions"][part] = dump_partition_camera_assets(part, img, args.out)
            except KeyError as e:
                print(f"[extract_ota] Skip: {e}", flush=True)
            finally:
                if img.exists():
                    img.unlink()

    (args.out / "_extract_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"[extract_ota] Done! Summary saved to {args.out / '_extract_summary.json'}", flush=True)


if __name__ == "__main__":
    main()
