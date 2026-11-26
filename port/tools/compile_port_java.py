#!/usr/bin/env python3
# Copyright (C) 2026 IRedDragonICY
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Compile clean Java port sources (java/com/onyx/port/*.java) into Smali and
remap all symbolic stub references to the dynamically discovered R8 symbols
of the current MiuiCamera APK.

Pipeline:
  1. javac --release 11 (against android.jar + java/stubs/)
  2. d8 --min-api 29 (desugars lambdas & Java 11 bytecode to DEX)
  3. baksmali (bundled inside apktool.jar) -> clean .smali files
  4. Rewrite symbolic stubs (__SYM_*__ & Lcom/onyx/stub/*;) -> live R8 names
  5. Copy only com/onyx/port/*.smali into smali_classes{N+1}/com/onyx/port/
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _find_android_jar(port_root: Path = None) -> Path:
    candidates = []
    sdk_root = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk_root:
        for api in (37, 36, 35, 34, 33):
            candidates.append(Path(sdk_root) / "platforms" / f"android-{api}" / "android.jar")
    if port_root is not None:
        for par in (port_root, *port_root.parents):
            psdk = par / "prebuilts" / "sdk"
            if psdk.is_dir():
                for api in (37, 36, 35, 34, 33):
                    candidates.append(psdk / str(api) / "public" / "android.jar")
                break
    candidates.extend([
        Path("/opt/android-sdk/platforms/android-36/android.jar"),
        Path("/opt/android-sdk/platforms/android-35/android.jar"),
        Path("/opt/android-sdk/platforms/android-34/android.jar"),
        Path("/home/ireddragonicy/Data/projects/onyx/los_workspace/src/prebuilts/sdk/36/public/android.jar"),
        Path("/home/ireddragonicy/Data/projects/onyx/los_workspace/src/prebuilts/sdk/35/public/android.jar"),
    ])
    for p in candidates:
        if p.is_file():
            return p
    plat = Path("/opt/android-sdk/platforms")
    if plat.is_dir():
        jars = sorted(plat.glob("android-*/android.jar"), reverse=True)
        if jars:
            return jars[0]
    raise RuntimeError("Could not find android.jar (set ANDROID_HOME or install Android SDK platform)")


def _find_d8() -> Path:
    for p in (
        shutil.which("d8"),
        "/opt/android-sdk/cmdline-tools/latest/bin/d8",
    ):
        if p and Path(p).is_file():
            return Path(p)
    bt = Path("/opt/android-sdk/build-tools")
    if bt.is_dir():
        found = sorted(bt.glob("*/d8"), reverse=True)
        if found:
            return found[0]
    raise RuntimeError("Could not find d8 compiler in PATH or /opt/android-sdk")


def _find_apktool_jar(port_root: Path) -> Path:
    candidates = []
    env_jar = os.environ.get("APKTOOL_JAR")
    if env_jar:
        candidates.append(Path(env_jar))
    candidates.extend([
        port_root / "tools" / "apktool.jar",
        port_root / "apktool.jar",
        Path("/home/ireddragonicy/Data/projects/onyx/nezha-camera-port/tools/apktool.jar"),
        Path("/home/ireddragonicy/Data/projects/onyx/miuicamera_patchwork/apktool.jar"),
        Path("/usr/share/java/apktool.jar"),
        Path("/usr/share/apktool/apktool.jar"),
    ])
    for p in candidates:
        if p.is_file():
            return p
    apktool_bin = shutil.which("apktool")
    if apktool_bin:
        try:
            txt = Path(apktool_bin).read_text(encoding="utf-8", errors="ignore")
            for tok in txt.split():
                tok_c = tok.strip("\"'")
                if tok_c.endswith(".jar") and Path(tok_c).is_file():
                    return Path(tok_c)
        except Exception:
            pass
    raise RuntimeError("Could not find apktool.jar (set APKTOOL_JAR or place it in port/tools/apktool.jar)")


def build_symbol_map(syms: dict) -> list[tuple[str, str]]:
    """Build deterministic (placeholder, target) remap list from discovered R8 symbols."""
    pairs = [
        # Class descriptors (longest/most-specific first)
        ("Lcom/onyx/stub/WrapperHolder;", syms["wrapper_holder_cls"]),
        ("Lcom/onyx/stub/Wrapper;", syms["wrapper_cls"]),
        ("Lcom/onyx/stub/WmManager;", syms["wm_mgr_cls"]),
        ("Lcom/onyx/stub/ZoomManager;", syms["zm_cls"]),
        ("Lcom/onyx/stub/ZoomProto;", syms["zoom_proto_cls"]),
        ("Lcom/onyx/stub/CamModule;", syms["module_iface"]),
        ("Lcom/onyx/stub/CamManager;", syms["cam_mgr_iface"]),
        ("Lcom/onyx/stub/CamCaps;", syms["caps_cls"]),
        ("Lcom/onyx/stub/RecorderParam;", syms["mrp_cls"]),
        ("Lcom/onyx/stub/StorageWriter;", syms["sw_cls"]),
        # Optional classes (fallback to safe no-op class if absent in a future APK)
        ("Lcom/onyx/stub/WmDevCache;", syms.get("wm_devcache_cls", "Lcom/onyx/stub/WmDevCache;")),
        ("Lcom/onyx/stub/WmUtils;", syms.get("wm_utils_cls", "Lcom/onyx/stub/WmUtils;")),
        # Preference & PreferenceGroup obfuscated members
        ("__SYM_SET_KEY__", syms["pref_set_key"]),
        ("__SYM_SET_TITLE__", syms["pref_set_title"]),
        ("__SYM_SET_SUMMARY__", syms["pref_set_summary"]),
        ("__SYM_KEY__", syms["pref_key_field"]),
        ("__SYM_PERSISTENT__", syms["pref_persistent_field"]),
        ("__SYM_ONCLICK__", syms["pref_onclick_field"]),
        ("__SYM_ADD_PREF__", syms["pg_add_pref"]),
        ("__SYM_FIND_PREF__", syms["pg_find_pref"]),
        # Watermark brand/manager obfuscated members
        ("__SYM_WRAPPER_INST__", syms["wrapper_holder_field"]),
        ("__SYM_WRAPPER_BRAND__", syms["wrapper_brand_m"]),
        ("__SYM_WRAPPER_DEVICE__", syms["wrapper_device_m"]),
        ("__SYM_WGP_BRAND__", syms["wgp_brand_field"]),
        ("__SYM_WGP_DEVICE__", syms["wgp_device_field"]),
        ("__SYM_WM_INIT__", syms["wm_mgr_init_field"]),
        ("__SYM_WM_GROUPS__", syms["wm_mgr_groups_field"]),
        ("__SYM_WM_CUR__", syms["wm_mgr_cur_field"]),
        ("__SYM_WM_LOAD__", syms["wm_mgr_load_m"]),
        ("__SYM_WM_GETCUR__", syms["wm_mgr_cur_m"]),
        ("__SYM_WM_ISVID__", syms["wm_mgr_isvid_m"]),
        ("__SYM_WM_MIVI__", syms["wm_item_mivi_m"]),
        ("__SYM_DEVCACHE_INST__", syms.get("wm_devcache_field", "a")),
        ("__SYM_WMUTILS_INIT__", syms.get("wm_utils_init_m", "a")),
        # Smooth Zoom obfuscated members
        ("__SYM_ZM_MOD__", syms["zm_mod_field"]),
        ("__SYM_ZP_OPT__", syms["zoom_proto_opt_m"]),
        ("__SYM_ZP_RNG__", syms["zoom_proto_rng_m"]),
        ("__SYM_CM_CAPS__", syms["cam_mgr_caps_m"]),
        ("__SYM_CAPS_MAX__", syms["caps_max_m"]),
        # Pro Mod MediaRecorderParameter obfuscated fields
        ("__SYM_MRP_ENA_AUD__", syms["mrp_enable_audio"]),
        ("__SYM_MRP_AUD_BR__", syms["mrp_audio_bitrate"]),
        ("__SYM_MRP_AUD_SR__", syms["mrp_audio_sr"]),
        ("__SYM_MRP_VID_BR__", syms["mrp_video_bitrate"]),
        ("__SYM_MRP_MAX_DUR__", syms["mrp_max_duration"]),
        ("__SYM_MRP_MAX_SIZE__", syms["mrp_max_filesize"]),
        # StorageWriter (TaskDataStorageWriter) original writeTo method
        ("__SYM_SW_ORIG__", syms["sw_orig_m"]),
        # Fragment title string resource IDs
        ("0x7f14eeee", syms["zoom_title_res"]),
        ("0x7f14eeef", syms["pro_title_res"]),
    ]
    return pairs


def compile_port_java_to_smali(port_root: Path, syms: dict, out_port_dir: Path) -> list[Path]:
    """
    Compile java/com/onyx/port/*.java -> DEX -> Smali, remap all R8 symbols,
    and write resulting .smali files directly into out_port_dir.
    """
    java_root = port_root / "java"
    stubs_dir = java_root / "stubs"
    src_dir = java_root / "com" / "onyx" / "port"
    if not src_dir.is_dir():
        raise RuntimeError(f"Java port sources not found at {src_dir}")

    android_jar = _find_android_jar(port_root)
    d8_bin = _find_d8()
    apktool_jar = _find_apktool_jar(port_root)

    src_files = sorted(src_dir.rglob("*.java"))
    stub_files = sorted(stubs_dir.rglob("*.java"))
    if not src_files:
        raise RuntimeError(f"No .java files in {src_dir}")

    with tempfile.TemporaryDirectory(prefix="onyx_port_javac_") as tmp:
        tmp_p = Path(tmp)
        classes_dir = tmp_p / "classes"
        dex_dir = tmp_p / "dex"
        smali_dir = tmp_p / "smali"
        classes_dir.mkdir()
        dex_dir.mkdir()
        smali_dir.mkdir()

        # 1. Compile Java sources + compile-time stubs with javac --release 11
        cmd_javac = [
            "javac",
            "--release", "11",
            "-encoding", "UTF-8",
            "-cp", str(android_jar),
            "-d", str(classes_dir),
            *[str(f) for f in stub_files],
            *[str(f) for f in src_files],
        ]
        res = subprocess.run(cmd_javac, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"javac failed:\n{res.stdout}\n{res.stderr}")

        # 2. Dex ONLY com/onyx/port/*.class (never pack compile-time stubs!)
        port_classes = sorted((classes_dir / "com" / "onyx" / "port").rglob("*.class"))
        cmd_d8 = [
            str(d8_bin),
            "--min-api", "29",
            "--lib", str(android_jar),
            "--classpath", str(classes_dir),
            "--output", str(dex_dir),
            *[str(c) for c in port_classes],
        ]
        res = subprocess.run(cmd_d8, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"d8 failed:\n{res.stdout}\n{res.stderr}")

        dex_file = dex_dir / "classes.dex"
        if not dex_file.is_file():
            raise RuntimeError("d8 did not produce classes.dex")

        # 3. Disassemble classes.dex -> Smali via baksmali API bundled inside apktool.jar
        runner_java = tmp_p / "DisassembleDex.java"
        runner_java.write_text(
            "import com.android.tools.smali.baksmali.Baksmali;\n"
            "import com.android.tools.smali.baksmali.BaksmaliOptions;\n"
            "import com.android.tools.smali.dexlib2.DexFileFactory;\n"
            "import com.android.tools.smali.dexlib2.dexbacked.DexBackedDexFile;\n"
            "import java.io.File;\n"
            "public class DisassembleDex {\n"
            "    public static void main(String[] args) throws Exception {\n"
            "        var c = DexFileFactory.loadDexContainer(new File(args[0]), null);\n"
            "        var entry = c.getEntry((String) c.getDexEntryNames().get(0));\n"
            "        var opts = new BaksmaliOptions();\n"
            "        opts.debugInfo = false;\n"
            "        Baksmali.disassembleDexFile((DexBackedDexFile) entry.getDexFile(), new File(args[1]), 4, opts);\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        cmd_bak = ["java", "-cp", str(apktool_jar), str(runner_java), str(dex_file), str(smali_dir)]
        res = subprocess.run(cmd_bak, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"baksmali failed:\n{res.stdout}\n{res.stderr}")

        # 4. Remap all symbolic stubs -> discovered R8 symbols & copy to out_port_dir
        remap = build_symbol_map(syms)
        gen_dir = smali_dir / "com" / "onyx" / "port"
        if not gen_dir.is_dir():
            raise RuntimeError("baksmali produced no com/onyx/port smali files")

        # Clean any stale generated smali in out_port_dir first
        if out_port_dir.exists():
            for old in out_port_dir.glob("*.smali"):
                old.unlink()
        out_port_dir.mkdir(parents=True, exist_ok=True)

        written = []
        for sf in sorted(gen_dir.glob("*.smali")):
            txt = sf.read_text(encoding="utf-8")
            for needle, repl in remap:
                txt = txt.replace(needle, repl)
            # Sanity check: ensure zero unresolved __SYM_ or com/onyx/stub tokens remain!
            if "__SYM_" in txt or "Lcom/onyx/stub/" in txt:
                raise RuntimeError(f"Unresolved stub symbol left in {sf.name}!")
            dst = out_port_dir / sf.name
            dst.write_text(txt, encoding="utf-8")
            written.append(dst)

    return written
