// Copyright (C) 2026 IRedDragonICY
// SPDX-License-Identifier: GPL-3.0-or-later

package com.onyx.port;

import android.animation.TimeInterpolator;
import android.animation.ValueAnimator;
import android.app.AlertDialog;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.res.AssetManager;
import android.media.ImageReader;
import android.net.Uri;
import android.util.Log;
import android.util.Range;
import android.view.animation.PathInterpolator;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.preference.CheckBoxPreference;
import androidx.preference.Preference;
import androidx.preference.PreferenceCategory;
import androidx.preference.PreferenceGroup;
import com.android.camera.fragment.settings.PreferenceExtraActivity;
import com.android.camera.fragment.settings.b;
import com.android.camera.fragment.watermark.wmSettingV2.WmGalleryFragment;
import com.android.camera.fragment.watermark.wmSettingV2.WmGalleryPreference;
import com.onyx.stub.CamCaps;
import com.onyx.stub.CamManager;
import com.onyx.stub.CamModule;
import com.onyx.stub.ModuleState;
import com.onyx.stub.RecorderParam;
import com.onyx.stub.StorageWriter;
import com.onyx.stub.WmDevCache;
import com.onyx.stub.WmManager;
import com.onyx.stub.WmUtils;
import com.onyx.stub.Wrapper;
import com.onyx.stub.WrapperHolder;
import com.onyx.stub.ZoomManager;
import com.onyx.stub.ZoomProto;
import com.xiaomi.cam.watermark.a;
import com.xiaomi.camera.basic.Global;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.ref.WeakReference;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Optional;
import miuix.preference.TextPreference;

/**
 * Runtime companion for the Nezha -> Onyx MiuiCamera port.
 * Compiled via javac -> d8 -> baksmali and symbol-remapped at patch time so
 * every obfuscated MiuiCamera reference resolves dynamically across APK updates.
 */
public final class OnyxPortHelper {
    private static final String TAG = "OnyxPortHelper";
    private static final String PREFS_NAME = "onyx_port_prefs";
    public static final String TELEGRAM_ARCHIVE_URL = "https://t.me/ireddragonicy_archive";
    public static final String TELEGRAM_ARCHIVE_DISPLAY = "t.me/ireddragonicy_archive";

    static final int DARK_DIALOG_THEME = 0x01030226; // Theme_Material_Dialog_Alert
    static final int COLOR_LABEL = 0xFFCCCCCC;
    static final int COLOR_TEXT = 0xFFFFFFFF;
    static final int COLOR_HINT = 0xFF888888;

    private static boolean sSmoothZoomInit;
    private static boolean sSmoothZoom;
    private static boolean sWmBrandInit;
    private static int sWmBrand;
    private static String sCustomBrand;
    private static String sCustomModel;
    private static int sCurveTag;
    private static TimeInterpolator sInterp;
    private static float sLoggedCap;
    private static float sLoggedLift;

    private OnyxPortHelper() {}

    public static SharedPreferences getPrefs() {
        try {
            Context ctx = Global.getContext();
            if (ctx == null) return null;
            return ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE);
        } catch (Throwable t) {
            return null;
        }
    }

    // =========================================================================
    // 1. Android 17 50MP Full-Frame ImageReader Fix
    // =========================================================================

    public static ImageReader createImageReader(int width, int height, int format, int maxImages) {
        if ((format == 0x100 || format == 0x1005 || format == 0x48454946)
                && (width >= 5000 || height >= 5000)) {
            try {
                // USAGE_CPU_READ_OFTEN | GRALLOC_USAGE_RENDERSCRIPT -> flexibleConsumer=false
                ImageReader ir = ImageReader.newInstance(width, height, format, maxImages, 0x100003L);
                Log.i(TAG, "50MP ImageReader usage=0x100003 " + width + "x" + height + " fmt=" + format);
                return ir;
            } catch (Throwable t) {
                Log.w(TAG, "50MP ImageReader 5-arg fallback", t);
            }
        }
        return ImageReader.newInstance(width, height, format, maxImages);
    }

    // =========================================================================
    // 2. Smooth Zoom (Mod) — 7 Bezier Curves, 6 Speeds, 10x+ Ratios & Video Zoom
    // =========================================================================

    public static boolean isSmoothZoomEnabled() {
        if (sSmoothZoomInit) return sSmoothZoom;
        SharedPreferences p = getPrefs();
        if (p == null) return true;
        sSmoothZoom = p.getBoolean("smooth_enable", true);
        sSmoothZoomInit = true;
        return sSmoothZoom;
    }

    public static void setSmoothZoomEnabled(boolean enabled) {
        sSmoothZoom = enabled;
        sSmoothZoomInit = true;
        SharedPreferences p = getPrefs();
        if (p != null) {
            p.edit().putBoolean("smooth_enable", enabled).apply();
        }
    }

    public static String speedLabel(int pct) {
        if (pct <= 100) return "1.0x (Stock)";
        if (pct <= 150) return "1.5x (Fast)";
        if (pct <= 200) return "2.0x (Snappy)";
        if (pct <= 300) return "3.0x (Default)";
        if (pct <= 450) return "4.5x (Cinematic)";
        return "6.0x (Ultra Slow)";
    }

    public static String curveLabel(int idx) {
        switch (idx) {
            case 0: return "Linear (Stock)";
            case 1: return "Standard (Material)";
            case 2: return "Cinematic S-Curve";
            case 3: return "Long-tail (Fast Start)";
            case 4: return "Silky Lens Switch (Default)";
            case 5: return "Cinema Dolly (Ultra Smooth)";
            case 6: return "Glide Ease (Cubic)";
            default: return "Silky Lens Switch (Default)";
        }
    }

    private static TimeInterpolator interpolatorFor(int idx) {
        int tag = idx + 1;
        if (tag == sCurveTag && sInterp != null) return sInterp;
        TimeInterpolator interp;
        switch (idx) {
            case 0: interp = null; break;
            case 1: interp = new PathInterpolator(0.40f, 0.00f, 0.20f, 1.00f); break;
            case 2: interp = new PathInterpolator(0.65f, 0.00f, 0.35f, 1.00f); break;
            case 3: interp = new PathInterpolator(0.16f, 1.00f, 0.30f, 1.00f); break;
            case 5: interp = new PathInterpolator(0.45f, 0.00f, 0.15f, 1.00f); break;
            case 6: interp = new PathInterpolator(0.33f, 1.00f, 0.68f, 1.00f); break;
            case 4:
            default:
                interp = new PathInterpolator(0.22f, 1.00f, 0.36f, 1.00f);
                break;
        }
        sCurveTag = tag;
        sInterp = interp;
        return interp;
    }

    public static void applyDuration(ValueAnimator anim) {
        if (anim == null) return;
        try {
            if (!isSmoothZoomEnabled()) return;
            SharedPreferences p = getPrefs();
            long dur = anim.getDuration();
            if (dur <= 0L) dur = 180L;
            int speedPct = (p != null) ? p.getInt("speed_pct", 300) : 300;
            dur = (dur * speedPct) / 100L;
            if (dur < 220L) dur = 220L;
            if (dur > 1400L) dur = 1400L;
            anim.setDuration(dur);
            int curve = (p != null) ? p.getInt("curve", 4) : 4;
            TimeInterpolator interp = interpolatorFor(curve);
            if (interp != null) {
                anim.setInterpolator(interp);
            }
        } catch (Throwable ignored) {}
    }

    public static boolean forcePerFrame(boolean stock) {
        return (!stock && isSmoothZoomEnabled()) ? true : stock;
    }

    public static float filterTargetZoom(float target) {
        return (target != 0.0f && isSmoothZoomEnabled()) ? 0.0f : target;
    }

    public static boolean collapseInRecord(boolean stock) {
        if (!stock) return false;
        try {
            SharedPreferences p = getPrefs();
            boolean keep = (p == null) || p.getBoolean("record_buttons", true);
            if (keep) return false;
        } catch (Throwable ignored) {}
        return stock;
    }

    private static Range<Float> sessionZoomRange() {
        try {
            Optional<ZoomProto> opt = ZoomProto.__SYM_ZP_OPT__();
            ZoomProto zp = opt.orElse(null);
            if (zp != null) {
                Range<Float> r = zp.__SYM_ZP_RNG__();
                if (r != null && r.getUpper() > 1.0f) return r;
            }
        } catch (Throwable ignored) {}
        return null;
    }

    public static float[] extendRatios(float[] base) {
        if (base == null) return null;
        boolean hasUw = false;
        for (float v : base) {
            if (v <= 0.6f) {
                hasUw = true;
                break;
            }
        }
        if (!hasUw) return base;

        SharedPreferences p = getPrefs();
        boolean enabled = (p == null) || p.getBoolean("extra_buttons", true);
        String csv = (p != null) ? p.getString("extra_ratios", "3.0,5.0,7.5,10.0") : "3.0,5.0,7.5,10.0";
        if (!enabled || csv == null) return base;

        String[] parts = csv.split(",");
        if (parts.length == 0) return base;

        float minZ = 0f;
        float maxZ = 0f;
        Range<Float> rng = sessionZoomRange();
        if (rng != null) {
            minZ = rng.getLower();
            maxZ = rng.getUpper();
        }
        if (maxZ != sLoggedCap) {
            sLoggedCap = maxZ;
            Log.d(TAG, "extendRatios: session zoom max = " + maxZ);
        }

        float[] out = new float[base.length + parts.length];
        System.arraycopy(base, 0, out, 0, base.length);
        int idx = base.length;
        for (String raw : parts) {
            float val;
            try {
                val = Float.parseFloat(raw.trim());
            } catch (NumberFormatException e) {
                val = 1.0f;
            }
            if (maxZ == 0f || (val <= maxZ && val >= minZ)) {
                out[idx++] = val;
            }
        }
        if (idx == out.length) return out;
        if (idx == base.length) return base;
        return Arrays.copyOf(out, idx);
    }

    public static Range<Float> widenZoomRange(ZoomManager zm, Range<Float> range) {
        if (range == null) return null;
        try {
            SharedPreferences p = getPrefs();
            boolean widen = (p == null) || p.getBoolean("video_max_zoom", true);
            if (!widen) return range;
            float curMax = range.getUpper();
            WeakReference<CamModule> ref = zm.__SYM_ZM_MOD__;
            CamModule mod = (ref != null) ? ref.get() : null;
            if (mod == null) return range;
            CamManager cm = mod.getCameraManager();
            if (cm == null) return range;
            CamCaps caps = cm.__SYM_CM_CAPS__();
            if (caps == null) return range;
            float sensorMax = caps.__SYM_CAPS_MAX__();
            if (curMax != sLoggedLift) {
                sLoggedLift = curMax;
                Log.d(TAG, "widenZoomRange: upper " + curMax + " -> sensorMax " + sensorMax);
            }
            if (sensorMax > curMax) {
                return Range.create(range.getLower(), sensorMax);
            }
        } catch (Throwable ignored) {}
        return range;
    }

    public static String zoomModSummary() {
        if (!isSmoothZoomEnabled()) return "Off (Stock zoom)";
        SharedPreferences p = getPrefs();
        int spd = (p != null) ? p.getInt("speed_pct", 300) : 300;
        int crv = (p != null) ? p.getInt("curve", 4) : 4;
        String rat = (p != null) ? p.getString("extra_ratios", "3.0,5.0,7.5,10.0") : "3.0,5.0,7.5,10.0";
        return "On \u2022 " + speedLabel(spd) + " \u2022 " + curveLabel(crv) + " (" + rat + ")";
    }

    public static void addZoomPreference(b frag, PreferenceCategory cat) {
        if (frag == null || cat == null) return;
        try {
            PreferenceGroup grp = cat;
            Preference existing = grp.__SYM_FIND_PREF__("pref_zoom_mod");
            if (existing != null) {
                existing.__SYM_SET_SUMMARY__(zoomModSummary());
                existing.__SYM_ONCLICK__ = pref -> {
                    openSettingsSubpage(frag, ZoomModSettingsFragment.class);
                    return true;
                };
                return;
            }
            Context ctx = frag.getContext();
            if (ctx == null) ctx = Global.getApplication();
            TextPreference pref = new TextPreference(ctx, null);
            pref.__SYM_SET_KEY__("pref_zoom_mod");
            pref.__SYM_SET_TITLE__("Smooth Zoom (Mod)");
            pref.__SYM_SET_SUMMARY__(zoomModSummary());
            pref.__SYM_PERSISTENT__ = false;
            pref.__SYM_ONCLICK__ = p -> {
                openSettingsSubpage(frag, ZoomModSettingsFragment.class);
                return true;
            };
            grp.__SYM_ADD_PREF__(pref);
        } catch (Throwable t) {
            Log.e(TAG, "addZoomPreference failed", t);
        }
    }

    public static void refreshZoomModSummary(PreferenceGroup grp) {
        if (grp == null) return;
        try {
            Preference p = grp.__SYM_FIND_PREF__("pref_zoom_mod");
            if (p != null) p.__SYM_SET_SUMMARY__(zoomModSummary());
            refreshProModSummary(grp);
            configureUpgradePreference(grp);
        } catch (Throwable ignored) {}
    }

    public static String getAppVersionName() {
        try {
            Context ctx = Global.getContext();
            if (ctx != null) {
                PackageInfo pi = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
                if (pi != null && pi.versionName != null) return pi.versionName;
            }
        } catch (Throwable ignored) {}
        return "6.7.000500.0_IRedDragonICY";
    }

    public static void openTelegramArchive(Context ctx) {
        try {
            Context c = (ctx != null) ? ctx : Global.getContext();
            if (c == null) return;
            Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(TELEGRAM_ARCHIVE_URL));
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            c.startActivity(intent);
        } catch (Throwable t) {
            Log.e(TAG, "openTelegramArchive failed", t);
        }
    }

    public static void configureUpgradePreference(PreferenceGroup grp) {
        if (grp == null) return;
        try {
            Preference upg = grp.__SYM_FIND_PREF__("pref_upgrade");
            if (upg != null) {
                upg.__SYM_SET_SUMMARY__("Version " + getAppVersionName() + " \u00b7 " + TELEGRAM_ARCHIVE_DISPLAY);
                upg.__SYM_ONCLICK__ = clicked -> {
                    openTelegramArchive(null);
                    return true;
                };
            }
        } catch (Throwable ignored) {}
    }

    // =========================================================================
    // 3. Camera Quality & Pro Mod — Bitrate, Audio, Custom JPEG %, Limits, Mute
    // =========================================================================

    public static int getVideoBitrateMbps() {
        SharedPreferences p = getPrefs();
        return (p != null) ? p.getInt("pro_video_bitrate", 0) : 0;
    }

    public static int getAudioBitrateKbps() {
        SharedPreferences p = getPrefs();
        return (p != null) ? p.getInt("pro_audio_bitrate", 0) : 0;
    }

    public static int getPhotoQualityPct() {
        SharedPreferences p = getPrefs();
        return (p != null) ? p.getInt("pro_photo_quality", 0) : 0;
    }

    public static boolean isUnlimitedDuration() {
        SharedPreferences p = getPrefs();
        return (p == null) || p.getBoolean("pro_unlimited_dur", true);
    }

    public static boolean isBypassThermal() {
        SharedPreferences p = getPrefs();
        return (p == null) || p.getBoolean("pro_bypass_thermal", true);
    }

    public static boolean isBypassLowBattery() {
        SharedPreferences p = getPrefs();
        return (p == null) || p.getBoolean("pro_bypass_battery", true);
    }

    public static boolean isForceMuteSound() {
        SharedPreferences p = getPrefs();
        return (p != null) && p.getBoolean("pro_force_mute", false);
    }

    /** True = encode 120/240fps slow-mo as standard 30fps HFR slow-motion (no audio). */
    public static boolean isSlowMoTrueHfr() {
        SharedPreferences p = getPrefs();
        return (p == null) || p.getBoolean("pro_slowmo_hfr", true);
    }

    public static String slowMoModeLabel(boolean hfr) {
        return hfr ? "30fps Slow-Mo (Default)" : "Stock HSR (120/240fps + Audio)";
    }

    /** 0 = Off when HDR is Off (Default), 1 = Always Off (SDR only), 2 = Stock (Always On). */
    public static int getUltraHdrMode() {
        SharedPreferences p = getPrefs();
        return (p != null) ? p.getInt("pro_ultrahdr_mode", 0) : 0;
    }

    public static String ultraHdrModeLabel(int mode) {
        switch (mode) {
            case 1: return "Always Off (SDR only)";
            case 2: return "Stock (Always On)";
            case 0:
            default: return "Off when HDR is Off (Default)";
        }
    }

    public static String videoBitrateLabel(int mbps) {
        return (mbps <= 0) ? "Stock" : (mbps + " Mbps");
    }

    public static String audioBitrateLabel(int kbps) {
        return (kbps <= 0) ? "Stock" : (kbps + " kbps (48 kHz)");
    }

    public static String photoQualityLabel(int pct) {
        if (pct <= 0) return "Stock";
        return (pct > 95) ? (pct + "% (50MP capped 95%)") : (pct + "%");
    }

    public static String proModSummary() {
        StringBuilder sb = new StringBuilder();
        int vid = getVideoBitrateMbps();
        sb.append("Vid: ").append(vid <= 0 ? "Stock" : (vid + "M"));
        int pic = getPhotoQualityPct();
        sb.append(" \u00b7 Pic: ").append(pic <= 0 ? "Stock" : (pic + "%"));
        if (isBypassThermal()) sb.append(" \u00b7 NoThermal");
        if (isUnlimitedDuration()) sb.append(" \u00b7 NoLimit");
        return sb.toString();
    }

    public static void applyVideoParams(RecorderParam param) {
        if (param == null) return;
        try {
            int curMod = ModuleState.__SYM_CUR_MOD__;
            boolean isSlowMo = (curMod == 0xAC);
            int origMaxDur = param.__SYM_MRP_MAX_DUR__;
            // Preserve short burst / auto-stop durations (< 60s): 960fps slow-mo (~2.25s),
            // 480/1920/3840fps, 960_direct (30s), short clips (10s), and Intent durationLimit.
            boolean isShortBurst = (origMaxDur > 0 && origMaxDur < 60_000);

            // Direct 120/240fps slow-mo (HSR): Xiaomi records real-time 120/240fps + audio
            // expecting MiuiGallery to slow it down. On AOSP / Google Photos, convert to
            // standard 30fps HFR slow-motion (4x / 8x) via CCodec InputSurfaceSource stretch.
            boolean isSlowMoDirect = isSlowMo
                    && !isShortBurst
                    && param.__SYM_MRP_ENA_AUD__
                    && param.__SYM_MRP_CAP_RATE__ >= 119.0
                    && param.__SYM_MRP_VID_FPS__ >= 119;
            if (isSlowMoDirect && isSlowMoTrueHfr()) {
                param.__SYM_MRP_VID_FPS__ = 30;
                param.__SYM_MRP_ENA_AUD__ = false;
                param.__SYM_MRP_AUD_SRC__ = -1;
                ArrayList<String> extras = param.__SYM_MRP_EXTRA__;
                if (extras != null) {
                    for (int i = 0; i < extras.size(); i++) {
                        String entry = extras.get(i);
                        if (entry != null && entry.startsWith("video-param-i-frames-interval=")) {
                            extras.set(i, "video-param-i-frames-interval=1.0");
                        }
                    }
                }
                Log.i(TAG, "applyVideoParams: slow-mo HFR cap=" + param.__SYM_MRP_CAP_RATE__
                        + " -> 30fps (audio off)");
            }

            int vidMbps = getVideoBitrateMbps();
            if (vidMbps > 0 && !isSlowMo) {
                int clamped = Math.max(5, Math.min(500, vidMbps));
                param.__SYM_MRP_VID_BR__ = clamped * 1_000_000;
            }
            int audKbps = getAudioBitrateKbps();
            if (audKbps > 0 && param.__SYM_MRP_ENA_AUD__) {
                param.__SYM_MRP_AUD_BR__ = audKbps * 1_000;
                param.__SYM_MRP_AUD_SR__ = 48_000;
            }
            boolean unlim = isUnlimitedDuration();
            if (unlim && !isShortBurst) {
                if (origMaxDur >= 60_000) {
                    param.__SYM_MRP_MAX_DUR__ = -1;
                }
                param.__SYM_MRP_MAX_SIZE__ = -1L;
            }
            if (vidMbps > 0 || audKbps > 0 || unlim || isSlowMo) {
                Log.i(TAG, "applyVideoParams: mod=" + curMod
                        + " fps=" + param.__SYM_MRP_VID_FPS__
                        + " cap=" + param.__SYM_MRP_CAP_RATE__
                        + " aud=" + param.__SYM_MRP_ENA_AUD__
                        + " maxDur=" + param.__SYM_MRP_MAX_DUR__);
            }
        } catch (Throwable t) {
            Log.w(TAG, "applyVideoParams failed", t);
        }
    }

    public static int applyPhotoQuality(int moduleIndex, int stockQuality) {
        int q = getPhotoQualityPct();
        if (q <= 0) return stockQuality;
        q = Math.max(1, Math.min(100, q));
        // Protect 50MP UltraHD MIVI output buffer from oversized JPEG truncation
        if (moduleIndex == 0xAF && q > 95) q = 95;
        return q;
    }

    public static void addProModPreference(b frag, PreferenceCategory cat) {
        if (frag == null || cat == null) return;
        try {
            PreferenceGroup grp = cat;
            Preference existing = grp.__SYM_FIND_PREF__("pref_pro_mod");
            if (existing != null) {
                existing.__SYM_SET_SUMMARY__(proModSummary());
                existing.__SYM_ONCLICK__ = pref -> {
                    openSettingsSubpage(frag, ProModSettingsFragment.class);
                    return true;
                };
                return;
            }
            Context ctx = frag.getContext();
            if (ctx == null) ctx = Global.getApplication();
            TextPreference pref = new TextPreference(ctx, null);
            pref.__SYM_SET_KEY__("pref_pro_mod");
            pref.__SYM_SET_TITLE__("Camera Quality & Pro Mod");
            pref.__SYM_SET_SUMMARY__(proModSummary());
            pref.__SYM_PERSISTENT__ = false;
            pref.__SYM_ONCLICK__ = p -> {
                openSettingsSubpage(frag, ProModSettingsFragment.class);
                return true;
            };
            grp.__SYM_ADD_PREF__(pref);
        } catch (Throwable t) {
            Log.e(TAG, "addProModPreference failed", t);
        }
    }

    public static void refreshProModSummary(PreferenceGroup grp) {
        if (grp == null) return;
        try {
            Preference p = grp.__SYM_FIND_PREF__("pref_pro_mod");
            if (p != null) p.__SYM_SET_SUMMARY__(proModSummary());
        } catch (Throwable ignored) {}
    }

    public static boolean handleZoomModClick(b frag, String key) {
        if (frag == null || key == null) return false;
        if ("pref_zoom_mod".equals(key)) {
            openSettingsSubpage(frag, ZoomModSettingsFragment.class);
            return true;
        }
        if ("pref_pro_mod".equals(key)) {
            openSettingsSubpage(frag, ProModSettingsFragment.class);
            return true;
        }
        if ("pref_upgrade".equals(key)) {
            openTelegramArchive(frag.getContext());
            return true;
        }
        return false;
    }

    private static void openSettingsSubpage(b frag, Class<?> fragCls) {
        try {
            frag.goToActivity(PreferenceExtraActivity.class, fragCls.getName());
        } catch (Throwable t) {
            Log.e(TAG, "openSettingsSubpage failed: " + fragCls.getSimpleName(), t);
        }
    }

    public static boolean syncCheckBoxPreference(CheckBoxPreference pref, String key) {
        if (!"pref_port_smooth_zoom".equals(key)) return false;
        pref.setChecked(isSmoothZoomEnabled());
        pref.__SYM_PERSISTENT__ = false;
        return true;
    }

    public static boolean handlePreferenceChange(Preference pref, Object newVal) {
        if (pref == null || !"pref_port_smooth_zoom".equals(pref.__SYM_KEY__)) return false;
        if (newVal instanceof Boolean) {
            setSmoothZoomEnabled((Boolean) newVal);
        }
        return true;
    }

    // =========================================================================
    // 4. Watermark Brand Switcher, Custom Phone Name & Live MIVI Logo Sync
    // =========================================================================

    public static int getWmBrandMode() {
        if (sWmBrandInit) return sWmBrand;
        SharedPreferences p = getPrefs();
        if (p == null) return 0;
        sWmBrand = p.getInt("pref_port_watermark_brand", 0);
        sWmBrandInit = true;
        return sWmBrand;
    }

    public static void setWmBrandMode(int mode) {
        sWmBrand = mode;
        sWmBrandInit = true;
        SharedPreferences p = getPrefs();
        if (p != null) {
            p.edit().putInt("pref_port_watermark_brand", mode).apply();
        }
    }

    public static String getCustomBrand() {
        if (sCustomBrand != null) return sCustomBrand;
        SharedPreferences p = getPrefs();
        String val = (p != null) ? p.getString("pref_port_custom_brand", "XIAOMI") : "XIAOMI";
        if (val == null || val.trim().isEmpty()) val = "XIAOMI";
        else val = val.trim();
        sCustomBrand = val;
        return val;
    }

    public static String getCustomModel() {
        if (sCustomModel != null) return sCustomModel;
        SharedPreferences p = getPrefs();
        String val = (p != null) ? p.getString("pref_port_custom_model", "Mi 17") : "Mi 17";
        if (val == null || val.trim().isEmpty()) val = "Mi 17";
        else val = val.trim();
        sCustomModel = val;
        return val;
    }

    public static void setCustomBrandModel(String brand, String model) {
        String bVal = (brand == null || brand.trim().isEmpty()) ? "XIAOMI" : brand.trim();
        String mVal = (model == null || model.trim().isEmpty()) ? "Mi 17" : model.trim();
        sCustomBrand = bVal;
        sCustomModel = mVal;
        sWmBrand = 3;
        sWmBrandInit = true;
        SharedPreferences p = getPrefs();
        if (p != null) {
            p.edit()
                    .putString("pref_port_custom_brand", bVal)
                    .putString("pref_port_custom_model", mVal)
                    .putInt("pref_port_watermark_brand", 3)
                    .apply();
        }
    }

    public static String getWmBrandLabel(int mode) {
        switch (mode) {
            case 1: return "Xiaomi 17 Ultra by Leica";
            case 2: return "LEITZPHONE powered by Xiaomi";
            case 3: return "Custom: " + getCustomBrand() + " " + getCustomModel();
            default: return "POCO F7 (MIUI Camera)";
        }
    }

    public static String[] getOverrideBrandArray() {
        int mode = getWmBrandMode();
        if (mode == 1) return new String[] { "XIAOMI", "17 Ultra by Leica" };
        if (mode == 2) return new String[] { "XIAOMI", "LEITZPHONE powered by Xiaomi" };
        if (mode == 3) return new String[] { getCustomBrand(), getCustomModel() };
        return null;
    }

    public static void addWatermarkStylePreference(WmGalleryFragment frag, PreferenceCategory cat) {
        if (frag == null || cat == null) return;
        try {
            Context ctx = frag.requireContext();
            Preference pref = new Preference(ctx, null);
            pref.__SYM_SET_KEY__("pref_port_watermark_brand");
            pref.__SYM_SET_TITLE__("Watermark style");
            pref.__SYM_SET_SUMMARY__(getWmBrandLabel(getWmBrandMode()));
            pref.__SYM_PERSISTENT__ = false;
            pref.__SYM_ONCLICK__ = clicked -> {
                showBrandPickerDialog(frag, clicked);
                return true;
            };
            ((PreferenceGroup) cat).__SYM_ADD_PREF__(pref);
        } catch (Throwable t) {
            Log.e(TAG, "addWatermarkStylePreference failed", t);
        }
    }

    private static void showBrandPickerDialog(WmGalleryFragment frag, Preference pref) {
        CharSequence[] items = {
                "POCO F7 (MIUI Camera)",
                "Xiaomi 17 Ultra by Leica",
                "LEITZPHONE powered by Xiaomi",
                "Custom phone name..."
        };
        new AlertDialog.Builder(frag.requireContext(), DARK_DIALOG_THEME)
                .setTitle("Watermark style")
                .setSingleChoiceItems(items, getWmBrandMode(), (dlg, which) -> {
                    dlg.dismiss();
                    if (which == 3) {
                        showCustomNameDialog(frag, pref);
                        return;
                    }
                    setWmBrandMode(which);
                    pref.__SYM_SET_SUMMARY__(getWmBrandLabel(which));
                    applyWatermarkBrandChange(frag);
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    public static void showCustomNameDialog(WmGalleryFragment frag, Preference pref) {
        if (frag == null) return;
        try {
            Context ctx = frag.requireContext();
            LinearLayout root = new LinearLayout(ctx);
            root.setOrientation(LinearLayout.VERTICAL);
            root.setPadding(48, 24, 48, 24);

            TextView brandLbl = new TextView(ctx);
            brandLbl.setText("Brand / Logo (XIAOMI, POCO, REDMI)");
            brandLbl.setTextColor(COLOR_LABEL);
            root.addView(brandLbl);

            EditText brandInput = new EditText(ctx);
            brandInput.setText(getCustomBrand());
            brandInput.setHint("XIAOMI");
            brandInput.setTextColor(COLOR_TEXT);
            brandInput.setHintTextColor(COLOR_HINT);
            brandInput.setSingleLine(true);
            root.addView(brandInput);

            TextView modelLbl = new TextView(ctx);
            modelLbl.setText("Phone model name (e.g. Mi 17, 17 Pro)");
            modelLbl.setTextColor(COLOR_LABEL);
            modelLbl.setPadding(0, 24, 0, 0);
            root.addView(modelLbl);

            EditText modelInput = new EditText(ctx);
            modelInput.setText(getCustomModel());
            modelInput.setHint("Mi 17");
            modelInput.setTextColor(COLOR_TEXT);
            modelInput.setHintTextColor(COLOR_HINT);
            modelInput.setSingleLine(true);
            root.addView(modelInput);

            new AlertDialog.Builder(ctx, DARK_DIALOG_THEME)
                    .setTitle("Custom phone name")
                    .setView(root)
                    .setPositiveButton("Apply", (dlg, which) -> {
                        setCustomBrandModel(brandInput.getText().toString(), modelInput.getText().toString());
                        pref.__SYM_SET_SUMMARY__(getWmBrandLabel(3));
                        applyWatermarkBrandChange(frag);
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
        } catch (Throwable t) {
            Log.e(TAG, "showCustomNameDialog failed", t);
        }
    }

    public static void applyWatermarkBrandChange(WmGalleryFragment frag) {
        if (frag == null) return;
        try {
            WmDevCache.__SYM_DEVCACHE_INST__ = null;
            Wrapper w = WrapperHolder.__SYM_WRAPPER_INST__;
            String brand = w.__SYM_WRAPPER_BRAND__();
            String device = w.__SYM_WRAPPER_DEVICE__();
            WmGalleryPreference wgp = frag.mWatermarkType;
            if (wgp != null) {
                wgp.__SYM_WGP_BRAND__ = brand;
                wgp.__SYM_WGP_DEVICE__ = device;
            }
            Context appCtx = frag.requireContext().getApplicationContext();
            WmManager mgr = frag.mWmManager;
            boolean isVid = (mgr != null) && mgr.__SYM_WM_ISVID__();
            WmUtils.__SYM_WMUTILS_INIT__(appCtx, isVid);
            if (mgr != null) {
                mgr.__SYM_WM_INIT__ = false;
                mgr.__SYM_WM_GROUPS__ = null;
                mgr.__SYM_WM_CUR__ = null;
                mgr.__SYM_WM_LOAD__(true);
                a cur = mgr.__SYM_WM_GETCUR__();
                if (cur != null) cur.__SYM_WM_MIVI__();
            }
            frag.refreshWmGallery();
        } catch (Throwable t) {
            Log.e(TAG, "applyWatermarkBrandChange failed", t);
        }
    }

    private static void copyStream(InputStream in, File dst) throws IOException {
        try (FileOutputStream out = new FileOutputStream(dst)) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) != -1) {
                out.write(buf, 0, n);
            }
            out.flush();
        }
    }

    private static void copyFile(File src, File dst) throws IOException {
        try (FileInputStream in = new FileInputStream(src)) {
            copyStream(in, dst);
        }
    }

    public static boolean prepareMiviBrandLogos(Path dirPath) {
        if (dirPath == null) return false;
        try {
            File dir = dirPath.toFile();
            if (!dir.isDirectory()) return false;
            int mode = getWmBrandMode();
            int markerByte = mode + '0';
            File marker = new File(dir, ".onyx_brand");
            if (marker.exists()) {
                try (FileInputStream in = new FileInputStream(marker)) {
                    if (in.read() == markerByte) return false;
                }
            }
            File[] files = dir.listFiles();
            if (mode == 1 || mode == 2) {
                if (files != null) {
                    for (File f : files) {
                        String name = f.getName();
                        if (name.startsWith("xiaomi_") && name.endsWith(".webp")) {
                            File backup = new File(dir, ".orig_" + name);
                            if (!backup.exists()) copyFile(f, backup);
                        }
                    }
                }
                Context ctx = Global.getContext();
                if (ctx != null) {
                    AssetManager am = ctx.getAssets();
                    String assetDir = (mode == 1)
                            ? "cloud_watermark_material/17_ultra_by_leica"
                            : "cloud_watermark_material/leitzphone_powered_by_xiaomi";
                    String[] list = am.list(assetDir);
                    if (list != null) {
                        for (String name : list) {
                            if (name.endsWith(".webp")) {
                                try (InputStream in = am.open(assetDir + "/" + name)) {
                                    copyStream(in, new File(dir, name));
                                }
                            }
                        }
                    }
                }
            } else if (files != null) {
                for (File f : files) {
                    String name = f.getName();
                    if (name.startsWith(".orig_xiaomi_")) {
                        copyFile(f, new File(dir, name.substring(6)));
                    }
                }
            }
            try (FileOutputStream out = new FileOutputStream(marker)) {
                out.write(markerByte);
            }
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "prepareMiviBrandLogos failed", t);
            return false;
        }
    }

    // =========================================================================
    // 5. Lossless Ultra HDR (GainMap) Filter when HDR is Off (Zero Re-Encode)
    // =========================================================================

    private static final byte[] EXIF_PREFIX = "Exif\0\0".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] XMP_PREFIX = "http://ns.adobe.com/xap/1.0/\0".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] MPF_PREFIX = "MPF\0".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] HDR_OFF_NEEDLE = "\"Hdr\":\"off\"".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] HDRGM_NEEDLE = "hdrgm".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] GAINMAP_NEEDLE = "GainMap".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] MOTION_NEEDLE = "MotionPhoto".getBytes(StandardCharsets.US_ASCII);
    private static final byte[] MICROVIDEO_NEEDLE = "MicroVideo".getBytes(StandardCharsets.US_ASCII);

    private static final class ExposedBaos extends ByteArrayOutputStream {
        ExposedBaos(int initialCapacity) {
            super(initialCapacity);
        }
        byte[] rawBuf() {
            return this.buf;
        }
        int rawCount() {
            return this.count;
        }
    }

    private static boolean startsWithBytes(byte[] data, int off, int len, byte[] prefix) {
        if (len < prefix.length) return false;
        for (int i = 0; i < prefix.length; i++) {
            if (data[off + i] != prefix[i]) return false;
        }
        return true;
    }

    private static int indexOfBytes(byte[] data, int off, int len, byte[] needle) {
        int nLen = needle.length;
        if (nLen == 0 || len < nLen) return -1;
        byte first = needle[0];
        int max = off + len - nLen;
        for (int i = off; i <= max; i++) {
            if (data[i] != first) continue;
            int j = 1;
            while (j < nLen && data[i + j] == needle[j]) j++;
            if (j == nLen) return i;
        }
        return -1;
    }

    /**
     * Bitmask returned by inspectJpegHeaders:
     *   bit 0 (1): hasGainMap (XMP hdrgm/GainMap or APP2 MPF)
     *   bit 1 (2): hasHdrOff  (EXIF JSON contains "Hdr":"off")
     *   bit 2 (4): hasMotionPhoto
     */
    private static int inspectJpegHeaders(byte[] data, int off, int len) {
        if (len < 16 || data[off] != (byte) 0xFF || data[off + 1] != (byte) 0xD8) {
            return 0;
        }
        int end = off + len;
        int p = off + 2;
        int flags = 0;
        while (p + 1 < end) {
            while (p < end && data[p] == (byte) 0xFF) p++;
            if (p >= end) break;
            int marker = data[p] & 0xFF;
            p++;
            if (marker == 0xDA || marker == 0xD9) break; // SOS or EOI
            if (marker == 0x00 || marker == 0x01 || (marker >= 0xD0 && marker <= 0xD8)) {
                continue;
            }
            if (p + 2 > end) break;
            int segLen = ((data[p] & 0xFF) << 8) | (data[p + 1] & 0xFF);
            if (segLen < 2 || p + segLen > end) break;
            int payOff = p + 2;
            int payLen = segLen - 2;
            if (marker == 0xE1) {
                if (startsWithBytes(data, payOff, payLen, EXIF_PREFIX)) {
                    if (indexOfBytes(data, payOff, payLen, HDR_OFF_NEEDLE) >= 0) {
                        flags |= 2;
                    }
                } else if (startsWithBytes(data, payOff, payLen, XMP_PREFIX)) {
                    if (indexOfBytes(data, payOff, payLen, HDRGM_NEEDLE) >= 0
                            || indexOfBytes(data, payOff, payLen, GAINMAP_NEEDLE) >= 0) {
                        flags |= 1;
                    }
                    if (indexOfBytes(data, payOff, payLen, MOTION_NEEDLE) >= 0
                            || indexOfBytes(data, payOff, payLen, MICROVIDEO_NEEDLE) >= 0) {
                        flags |= 4;
                    }
                }
            } else if (marker == 0xE2) {
                if (startsWithBytes(data, payOff, payLen, MPF_PREFIX)) {
                    flags |= 1;
                }
            }
            p += segLen;
        }
        return flags;
    }

    /** Walk a JPEG stream starting at `start` (0xFF 0xD8) and return index right after its EOI (0xFF 0xD9), or -1. */
    private static int findJpegEnd(byte[] data, int start, int end) {
        if (start + 4 > end || data[start] != (byte) 0xFF || data[start + 1] != (byte) 0xD8) {
            return -1;
        }
        int p = start + 2;
        while (p + 1 < end) {
            if (data[p] != (byte) 0xFF) return -1;
            while (p + 1 < end && data[p + 1] == (byte) 0xFF) p++;
            if (p + 1 >= end) return -1;
            int marker = data[p + 1] & 0xFF;
            if (marker == 0xD9) {
                return p + 2;
            }
            if (marker == 0x01 || (marker >= 0xD0 && marker <= 0xD8)) {
                p += 2;
                continue;
            }
            if (p + 4 > end) return -1;
            int segLen = ((data[p + 2] & 0xFF) << 8) | (data[p + 3] & 0xFF);
            if (segLen < 2 || p + 2 + segLen > end) return -1;
            if (marker == 0xDA) {
                // Scan entropy-coded data past byte-stuffing (FF 00) and RST0..RST7 (FF D0..D7)
                int i = p + 2 + segLen;
                boolean foundNext = false;
                while (i + 1 < end) {
                    if (data[i] == (byte) 0xFF) {
                        int j = i + 1;
                        while (j < end && data[j] == (byte) 0xFF) j++;
                        if (j >= end) return -1;
                        int nextM = data[j] & 0xFF;
                        if (nextM == 0x00 || (nextM >= 0xD0 && nextM <= 0xD7)) {
                            i = j + 1;
                            continue;
                        }
                        p = j - 1;
                        foundNext = true;
                        break;
                    }
                    i++;
                }
                if (!foundNext) return -1;
            } else {
                p += 2 + segLen;
            }
        }
        return -1;
    }

    private static boolean shouldStrip(int flags, int mode) {
        if ((flags & 1) == 0) return false; // No GainMap / MPF present
        if (mode == 1) return true;          // Always Off (SDR only)
        return (flags & 2) != 0;             // Mode 0: strip when EXIF records "Hdr":"off"
    }

    private static String stripGainMapFromMotionXmp(String xmp) {
        String s = xmp.replaceAll("(?s)<rdf:li[^>]*>[^<]*?(?:<[^>]+>[^<]*?)*?GainMap(?:[^<]*?<[^>]+>)*?[^<]*?</rdf:li>", "");
        s = s.replaceAll("\\s+hdrgm:[A-Za-z0-9_]+=\"[^\"]*\"", "");
        s = s.replaceAll("\\s+xmlns:hdrgm=\"[^\"]*\"", "");
        return s;
    }

    /**
     * Losslessly stream `data[off .. off+len)` to `out`, stripping Ultra HDR XMP hdrgm,
     * APP2 MPF directory, and trailing GainMap JPEG while preserving EXIF, ICC profile,
     * Watermark, primary JPEG scan, and any MotionPhoto MP4 payload.
     * Returns true if stripped, false if written unchanged.
     */
    private static boolean writeFilteredJpeg(byte[] data, int off, int len, int mode, OutputStream out) throws IOException {
        int flags = inspectJpegHeaders(data, off, len);
        if (!shouldStrip(flags, mode)) {
            out.write(data, off, len);
            return false;
        }
        int end = off + len;
        int primaryEnd = findJpegEnd(data, off, end);
        if (primaryEnd <= off) {
            out.write(data, off, len);
            return false;
        }
        boolean isMotion = (flags & 4) != 0;
        int mp4Start = -1;
        if (isMotion) {
            if (primaryEnd + 4 < end
                    && data[primaryEnd] == (byte) 0xFF
                    && data[primaryEnd + 1] == (byte) 0xD8) {
                int secEnd = findJpegEnd(data, primaryEnd, end);
                if (secEnd > primaryEnd && secEnd < end) {
                    mp4Start = secEnd;
                } else {
                    out.write(data, off, len);
                    return false;
                }
            } else {
                out.write(data, off, len);
                return false;
            }
        }

        // Write SOI
        out.write(0xFF);
        out.write(0xD8);
        int written = 2;
        int p = off + 2;
        while (p + 1 < primaryEnd) {
            int segStart = p;
            if (data[p] != (byte) 0xFF) break;
            while (p + 1 < primaryEnd && data[p + 1] == (byte) 0xFF) p++;
            if (p + 1 >= primaryEnd) break;
            int marker = data[p + 1] & 0xFF;
            if (marker == 0xDA || marker == 0xD9) {
                // Copy from first SOS through primary EOI in one shot (supports baseline + progressive)
                int rem = primaryEnd - segStart;
                out.write(data, segStart, rem);
                written += rem;
                break;
            }
            if (marker == 0x01 || (marker >= 0xD0 && marker <= 0xD8)) {
                int segTotal = (p + 2) - segStart;
                out.write(data, segStart, segTotal);
                written += segTotal;
                p += 2;
                continue;
            }
            int segLen = ((data[p + 2] & 0xFF) << 8) | (data[p + 3] & 0xFF);
            int nextP = p + 2 + segLen;
            int payOff = p + 4;
            int payLen = segLen - 2;
            boolean drop = false;
            if (marker == 0xE2 && startsWithBytes(data, payOff, payLen, MPF_PREFIX)) {
                drop = true;
            } else if (marker == 0xE1 && startsWithBytes(data, payOff, payLen, XMP_PREFIX)) {
                boolean hasGm = indexOfBytes(data, payOff, payLen, HDRGM_NEEDLE) >= 0
                        || indexOfBytes(data, payOff, payLen, GAINMAP_NEEDLE) >= 0;
                if (hasGm) {
                    if (!isMotion) {
                        drop = true;
                    } else {
                        String origXmp = new String(
                                data, payOff + XMP_PREFIX.length, payLen - XMP_PREFIX.length, StandardCharsets.UTF_8);
                        byte[] newXmpBytes = stripGainMapFromMotionXmp(origXmp).getBytes(StandardCharsets.UTF_8);
                        int newSegLen = 2 + XMP_PREFIX.length + newXmpBytes.length;
                        if (newSegLen <= 0xFFFF) {
                            out.write(0xFF);
                            out.write(0xE1);
                            out.write((newSegLen >>> 8) & 0xFF);
                            out.write(newSegLen & 0xFF);
                            out.write(XMP_PREFIX);
                            out.write(newXmpBytes);
                            written += 2 + newSegLen;
                            drop = true;
                        }
                    }
                }
            }
            if (!drop) {
                int segTotal = nextP - segStart;
                out.write(data, segStart, segTotal);
                written += segTotal;
            }
            p = nextP;
        }
        if (mp4Start > 0 && mp4Start < end) {
            int mp4Len = end - mp4Start;
            out.write(data, mp4Start, mp4Len);
            written += mp4Len;
        }
        Log.i(TAG, "UltraHDR stripped (mode=" + mode + "): " + len + " -> " + written + " bytes");
        return true;
    }

    public static ByteBuffer filterUltraHdrBuffer(ByteBuffer buf) {
        if (buf == null) return null;
        try {
            int mode = getUltraHdrMode();
            if (mode == 2) return buf;
            int limit = buf.limit();
            if (limit < 16) return buf;
            if (buf.hasArray()) {
                byte[] arr = buf.array();
                int off = buf.arrayOffset();
                int flags = inspectJpegHeaders(arr, off, limit);
                if (!shouldStrip(flags, mode)) return buf;
                ExposedBaos baos = new ExposedBaos(limit);
                if (!writeFilteredJpeg(arr, off, limit, mode, baos)) return buf;
                return ByteBuffer.wrap(baos.toByteArray());
            } else {
                // Direct ByteBuffer: inspect first 128 KB before allocating full buffer
                int headLen = Math.min(limit, 131072);
                byte[] head = new byte[headLen];
                ByteBuffer dup = buf.duplicate();
                dup.position(0);
                dup.get(head, 0, headLen);
                int flags = inspectJpegHeaders(head, 0, headLen);
                if (!shouldStrip(flags, mode)) return buf;
                byte[] full = new byte[limit];
                dup.position(0);
                dup.get(full, 0, limit);
                ExposedBaos baos = new ExposedBaos(limit);
                if (!writeFilteredJpeg(full, 0, limit, mode, baos)) return buf;
                return ByteBuffer.wrap(baos.toByteArray());
            }
        } catch (Throwable t) {
            Log.w(TAG, "filterUltraHdrBuffer fallback", t);
            return buf;
        }
    }

    public static void writeFilteredStream(StorageWriter writer, OutputStream out) throws IOException {
        if (writer == null || out == null) return;
        int mode = getUltraHdrMode();
        if (mode == 2) {
            writer.__SYM_SW_ORIG__(out);
            return;
        }
        ExposedBaos baos = new ExposedBaos(262144);
        writer.__SYM_SW_ORIG__(baos);
        byte[] raw = baos.rawBuf();
        int len = baos.rawCount();
        try {
            writeFilteredJpeg(raw, 0, len, mode, out);
        } catch (Throwable t) {
            Log.w(TAG, "writeFilteredStream fallback", t);
            out.write(raw, 0, len);
        }
    }
}
