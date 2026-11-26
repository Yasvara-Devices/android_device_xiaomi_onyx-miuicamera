// Copyright (C) 2026 IRedDragonICY
// SPDX-License-Identifier: GPL-3.0-or-later

package com.onyx.port;

import android.app.AlertDialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.text.InputType;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import androidx.preference.Preference;
import androidx.preference.PreferenceGroup;
import com.android.camera.fragment.settings.b;
import miuix.preference.TextPreference;

/**
 * Camera Quality & Pro Mod settings screen:
 *  - Video bitrate override (Stock / presets / custom Mbps)
 *  - Audio bitrate override (Stock / 192..512 kbps AAC @ 48 kHz)
 *  - Custom Photo JPEG / HEIC quality (1-100%, 0 = Stock; 50MP auto-capped <=95%)
 *  - Unlimited video recording duration & file size
 *  - Thermal & Slow-Motion overheat limit bypass
 *  - Low-battery flash & recording limit bypass
 *  - Force mute shutter & video start/stop sounds
 */
public class ProModSettingsFragment extends b {
    private static final String[] VIDEO_BITRATE_LABELS = {
            "Stock", "35 Mbps", "50 Mbps", "80 Mbps", "100 Mbps",
            "120 Mbps", "150 Mbps", "200 Mbps", "Custom (Mbps)..."
    };
    private static final int[] VIDEO_BITRATE_VALUES = { 0, 35, 50, 80, 100, 120, 150, 200 };

    private static final String[] AUDIO_BITRATE_LABELS = {
            "Stock", "192 kbps (48 kHz)", "256 kbps (48 kHz)", "320 kbps (48 kHz)", "512 kbps (48 kHz)"
    };
    private static final int[] AUDIO_BITRATE_VALUES = { 0, 192, 256, 320, 512 };

    private static final String[] ULTRA_HDR_LABELS = {
            "Off when HDR is Off (Default)",
            "Always Off (SDR only)",
            "Stock (Always On)"
    };

    private void addEntry(String key, String title, String summary) {
        TextPreference pref = new TextPreference(requireContext(), null);
        pref.__SYM_SET_KEY__(key);
        pref.__SYM_SET_TITLE__(title);
        pref.__SYM_SET_SUMMARY__(summary);
        pref.__SYM_PERSISTENT__ = false;
        pref.__SYM_ONCLICK__ = this;
        ((PreferenceGroup) mPreferenceGroup).__SYM_ADD_PREF__(pref);
    }

    private static String onOff(boolean v) {
        return v ? "On" : "Off";
    }

    private void setSummary(String key, String text) {
        Preference p = findPreference(key);
        if (p != null) p.__SYM_SET_SUMMARY__(text);
    }

    public void refreshSummaries() {
        setSummary("pref_pro_vid_br", OnyxPortHelper.videoBitrateLabel(OnyxPortHelper.getVideoBitrateMbps()));
        setSummary("pref_pro_aud_br", OnyxPortHelper.audioBitrateLabel(OnyxPortHelper.getAudioBitrateKbps()));
        setSummary("pref_pro_pic_q", OnyxPortHelper.photoQualityLabel(OnyxPortHelper.getPhotoQualityPct()));
        setSummary("pref_pro_ultrahdr", OnyxPortHelper.ultraHdrModeLabel(OnyxPortHelper.getUltraHdrMode()));
        setSummary("pref_pro_slowmo_hfr", OnyxPortHelper.slowMoModeLabel(OnyxPortHelper.isSlowMoTrueHfr()));
        setSummary("pref_pro_unlim_dur", onOff(OnyxPortHelper.isUnlimitedDuration()));
        setSummary("pref_pro_thermal", onOff(OnyxPortHelper.isBypassThermal()));
        setSummary("pref_pro_battery", onOff(OnyxPortHelper.isBypassLowBattery()));
        setSummary("pref_pro_mute", onOff(OnyxPortHelper.isForceMuteSound()));
        setSummary("pref_pro_about", OnyxPortHelper.getAppVersionName() + " \u00b7 " + OnyxPortHelper.TELEGRAM_ARCHIVE_DISPLAY);
    }

    @Override
    public void addCurrentPreferences() {
        addEntry("pref_pro_vid_br", "Video bitrate", "Stock");
        addEntry("pref_pro_aud_br", "Audio bitrate (AAC 48 kHz)", "Stock");
        addEntry("pref_pro_pic_q", "Photo JPEG / HEIC quality (%)", "Stock");
        addEntry("pref_pro_ultrahdr", "Ultra HDR (GainMap) mode", "Off when HDR is Off (Default)");
        addEntry("pref_pro_slowmo_hfr", "Slow-Mo 120/240fps output", "30fps Slow-Mo (Default)");
        addEntry("pref_pro_unlim_dur", "Unlimited video recording duration", "On");
        addEntry("pref_pro_thermal", "Bypass thermal & Slow-Mo overheat limit", "On");
        addEntry("pref_pro_battery", "Bypass low-battery flash & video limit", "On");
        addEntry("pref_pro_mute", "Force mute shutter & video sounds", "Off");
        addEntry("pref_pro_about", "Mod by _IRedDragonICY (Telegram)", OnyxPortHelper.TELEGRAM_ARCHIVE_DISPLAY);
        refreshSummaries();
    }

    @Override
    public int getFragmentTitle() {
        return 0x7f14eeef; // Replaced with discovered Pro/Settings title at patch time
    }

    @Override
    public boolean onPreferenceClick(Preference pref) {
        String key = pref.__SYM_KEY__;
        SharedPreferences p = OnyxPortHelper.getPrefs();
        if (key == null || p == null) return false;

        switch (key) {
            case "pref_pro_pic_q":
                showNumberDialog(true);
                return true;
            case "pref_pro_ultrahdr":
                new AlertDialog.Builder(requireContext(), OnyxPortHelper.DARK_DIALOG_THEME)
                        .setTitle("Ultra HDR (GainMap) mode")
                        .setSingleChoiceItems(ULTRA_HDR_LABELS, OnyxPortHelper.getUltraHdrMode(), (d, which) -> {
                            d.dismiss();
                            p.edit().putInt("pro_ultrahdr_mode", which).apply();
                            refreshSummaries();
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
                return true;
            case "pref_pro_slowmo_hfr":
                toggleBool(p, "pro_slowmo_hfr", true);
                return true;
            case "pref_pro_unlim_dur":
                toggleBool(p, "pro_unlimited_dur", true);
                return true;
            case "pref_pro_thermal":
                toggleBool(p, "pro_bypass_thermal", true);
                return true;
            case "pref_pro_battery":
                toggleBool(p, "pro_bypass_battery", true);
                return true;
            case "pref_pro_mute":
                toggleBool(p, "pro_force_mute", false);
                return true;
            case "pref_pro_about":
                OnyxPortHelper.openTelegramArchive(requireContext());
                return true;
            case "pref_pro_vid_br":
                int curV = OnyxPortHelper.getVideoBitrateMbps();
                int selV = VIDEO_BITRATE_LABELS.length - 1;
                for (int i = 0; i < VIDEO_BITRATE_VALUES.length; i++) {
                    if (curV == VIDEO_BITRATE_VALUES[i]) {
                        selV = i;
                        break;
                    }
                }
                new AlertDialog.Builder(requireContext(), OnyxPortHelper.DARK_DIALOG_THEME)
                        .setTitle("Video bitrate")
                        .setSingleChoiceItems(VIDEO_BITRATE_LABELS, selV, (d, which) -> {
                            d.dismiss();
                            if (which == VIDEO_BITRATE_LABELS.length - 1) {
                                showNumberDialog(false);
                                return;
                            }
                            int val = (which >= 0 && which < VIDEO_BITRATE_VALUES.length)
                                    ? VIDEO_BITRATE_VALUES[which] : 0;
                            p.edit().putInt("pro_video_bitrate", val).apply();
                            refreshSummaries();
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
                return true;
            case "pref_pro_aud_br":
                int curA = OnyxPortHelper.getAudioBitrateKbps();
                int selA = 0;
                for (int i = 0; i < AUDIO_BITRATE_VALUES.length; i++) {
                    if (curA <= AUDIO_BITRATE_VALUES[i]) {
                        selA = i;
                        break;
                    }
                    selA = i;
                }
                new AlertDialog.Builder(requireContext(), OnyxPortHelper.DARK_DIALOG_THEME)
                        .setTitle("Audio bitrate (AAC 48 kHz)")
                        .setSingleChoiceItems(AUDIO_BITRATE_LABELS, selA, (d, which) -> {
                            d.dismiss();
                            int val = (which >= 0 && which < AUDIO_BITRATE_VALUES.length)
                                    ? AUDIO_BITRATE_VALUES[which] : 0;
                            p.edit().putInt("pro_audio_bitrate", val).apply();
                            refreshSummaries();
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
                return true;
            default:
                return false;
        }
    }

    private void showNumberDialog(boolean isPhoto) {
        Context ctx = requireContext();
        LinearLayout root = new LinearLayout(ctx);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(60, 24, 60, 12);

        TextView msg = new TextView(ctx);
        msg.setText(isPhoto
                ? "Enter quality 1-100% (0 = Stock). 50MP auto-capped at 95% for stability."
                : "Enter custom video bitrate in Mbps (5-500, 0 = Stock).");
        msg.setTextSize(13f);
        msg.setTextColor(OnyxPortHelper.COLOR_LABEL);
        msg.setPadding(0, 0, 0, 12);
        root.addView(msg);

        EditText input = new EditText(ctx);
        input.setInputType(InputType.TYPE_CLASS_NUMBER);
        input.setSingleLine(true);
        input.setTextColor(OnyxPortHelper.COLOR_TEXT);
        input.setHintTextColor(OnyxPortHelper.COLOR_HINT);
        input.setHint(isPhoto ? "e.g. 98 (0 = Stock)" : "e.g. 120 (0 = Stock)");
        int cur = isPhoto ? OnyxPortHelper.getPhotoQualityPct() : OnyxPortHelper.getVideoBitrateMbps();
        if (cur > 0) {
            String s = String.valueOf(cur);
            input.setText(s);
            input.setSelection(s.length());
        }
        root.addView(input);

        String prefKey = isPhoto ? "pro_photo_quality" : "pro_video_bitrate";
        new AlertDialog.Builder(ctx, OnyxPortHelper.DARK_DIALOG_THEME)
                .setTitle(isPhoto ? "Photo JPEG / HEIC quality (%)" : "Custom video bitrate (Mbps)")
                .setView(root)
                .setPositiveButton("Apply", (d, w) -> {
                    SharedPreferences p = OnyxPortHelper.getPrefs();
                    if (p == null) return;
                    try {
                        String raw = input.getText().toString().trim();
                        int val = raw.isEmpty() ? 0 : Integer.parseInt(raw);
                        if (val < 0) val = 0;
                        if (isPhoto) {
                            val = Math.min(100, val);
                        } else if (val > 0) {
                            val = Math.max(5, Math.min(500, val));
                        }
                        p.edit().putInt(prefKey, val).apply();
                        refreshSummaries();
                    } catch (Throwable ignored) {}
                })
                .setNeutralButton("Reset Stock", (d, w) -> {
                    SharedPreferences p = OnyxPortHelper.getPrefs();
                    if (p == null) return;
                    p.edit().putInt(prefKey, 0).apply();
                    refreshSummaries();
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void toggleBool(SharedPreferences p, String key, boolean def) {
        p.edit().putBoolean(key, !p.getBoolean(key, def)).apply();
        refreshSummaries();
    }

    @Override
    public void onResume() {
        super.onResume();
        refreshSummaries();
    }

    @Override
    public void registerPreferenceListener() {}
}
