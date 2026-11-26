// Copyright (C) 2026 IRedDragonICY
// SPDX-License-Identifier: GPL-3.0-or-later

package com.onyx.port;

import android.app.AlertDialog;
import android.content.SharedPreferences;
import androidx.preference.Preference;
import androidx.preference.PreferenceGroup;
import com.android.camera.fragment.settings.b;
import miuix.preference.TextPreference;

/**
 * Full Smooth Zoom (Mod) subpage: 7 tuned Bezier curves, 6 speeds, extra 10x+
 * zoom ratio buttons, keep buttons while recording, and full video zoom range.
 */
public class ZoomModSettingsFragment extends b {
    private static final String[] CURVE_NAMES = {
            "Linear (Stock)",
            "Standard (Material)",
            "Cinematic S-Curve",
            "Long-tail (Fast Start)",
            "Silky Lens Switch (Default)",
            "Cinema Dolly (Ultra Smooth)",
            "Glide Ease (Cubic)"
    };

    private static final String[] SPEED_NAMES = {
            "1.0x (Stock)",
            "1.5x (Fast)",
            "2.0x (Snappy)",
            "3.0x (Default)",
            "4.5x (Cinematic)",
            "6.0x (Ultra Slow)"
    };
    private static final int[] SPEED_VALUES = { 100, 150, 200, 300, 450, 600 };

    private static final String[] RATIO_PRESETS = {
            "3.0,5.0,7.5,10.0",
            "2.0,3.0,5.0,10.0",
            "2.0,3.0,5.0",
            "4.3,6.7,10.0",
            "2.0,3.0,5.0,7.5,10.0,15.0"
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
        SharedPreferences p = OnyxPortHelper.getPrefs();
        if (p == null) return;
        setSummary("pref_zoom_mod_smooth", onOff(OnyxPortHelper.isSmoothZoomEnabled()));
        setSummary("pref_zoom_mod_curve", OnyxPortHelper.curveLabel(p.getInt("curve", 4)));
        setSummary("pref_zoom_mod_speed", OnyxPortHelper.speedLabel(p.getInt("speed_pct", 300)));
        setSummary("pref_zoom_mod_buttons", onOff(p.getBoolean("extra_buttons", true)));
        setSummary("pref_zoom_mod_ratios", p.getString("extra_ratios", "3.0,5.0,7.5,10.0"));
        setSummary("pref_zoom_mod_recbtn", onOff(p.getBoolean("record_buttons", true)));
        setSummary("pref_zoom_mod_vidzoom", onOff(p.getBoolean("video_max_zoom", true)));
    }

    @Override
    public void addCurrentPreferences() {
        addEntry("pref_zoom_mod_smooth", "Smooth zoom animation", "On");
        addEntry("pref_zoom_mod_curve", "Zoom curve / mood", "Silky Lens Switch (Default)");
        addEntry("pref_zoom_mod_speed", "Zoom speed", "3.0x (Default)");
        addEntry("pref_zoom_mod_buttons", "Extra zoom buttons (up to 10x)", "On");
        addEntry("pref_zoom_mod_ratios", "Extra zoom ratios preset", "3.0,5.0,7.5,10.0");
        addEntry("pref_zoom_mod_recbtn", "Keep zoom buttons while recording", "On");
        addEntry("pref_zoom_mod_vidzoom", "Full zoom range in video (10x+)", "On");
        refreshSummaries();
    }

    @Override
    public int getFragmentTitle() {
        return 0x7f14eeee; // Replaced with discovered parameter_zoom_slider_title at patch time
    }

    @Override
    public boolean onPreferenceClick(Preference pref) {
        String key = pref.__SYM_KEY__;
        SharedPreferences p = OnyxPortHelper.getPrefs();
        if (key == null || p == null) return false;

        switch (key) {
            case "pref_zoom_mod_smooth":
                OnyxPortHelper.setSmoothZoomEnabled(!OnyxPortHelper.isSmoothZoomEnabled());
                refreshSummaries();
                return true;
            case "pref_zoom_mod_buttons":
                toggleBool(p, "extra_buttons", true);
                return true;
            case "pref_zoom_mod_recbtn":
                toggleBool(p, "record_buttons", true);
                return true;
            case "pref_zoom_mod_vidzoom":
                toggleBool(p, "video_max_zoom", true);
                return true;
            case "pref_zoom_mod_curve":
                new AlertDialog.Builder(requireContext(), OnyxPortHelper.DARK_DIALOG_THEME)
                        .setTitle("Zoom curve / mood")
                        .setSingleChoiceItems(CURVE_NAMES, p.getInt("curve", 4), (d, which) -> {
                            d.dismiss();
                            p.edit().putInt("curve", which).apply();
                            refreshSummaries();
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
                return true;
            case "pref_zoom_mod_speed":
                int curSpd = p.getInt("speed_pct", 300);
                int selSpd = 3;
                for (int i = 0; i < SPEED_VALUES.length; i++) {
                    if (curSpd <= SPEED_VALUES[i]) {
                        selSpd = i;
                        break;
                    }
                }
                new AlertDialog.Builder(requireContext(), OnyxPortHelper.DARK_DIALOG_THEME)
                        .setTitle("Zoom speed")
                        .setSingleChoiceItems(SPEED_NAMES, selSpd, (d, which) -> {
                            d.dismiss();
                            int val = (which >= 0 && which < SPEED_VALUES.length) ? SPEED_VALUES[which] : 300;
                            p.edit().putInt("speed_pct", val).apply();
                            refreshSummaries();
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
                return true;
            case "pref_zoom_mod_ratios":
                String curRat = p.getString("extra_ratios", "3.0,5.0,7.5,10.0");
                int selRat = 0;
                for (int i = 0; i < RATIO_PRESETS.length; i++) {
                    if (RATIO_PRESETS[i].equals(curRat)) {
                        selRat = i;
                        break;
                    }
                }
                new AlertDialog.Builder(requireContext(), OnyxPortHelper.DARK_DIALOG_THEME)
                        .setTitle("Extra zoom ratios preset")
                        .setSingleChoiceItems(RATIO_PRESETS, selRat, (d, which) -> {
                            d.dismiss();
                            String val = (which >= 0 && which < RATIO_PRESETS.length)
                                    ? RATIO_PRESETS[which] : RATIO_PRESETS[0];
                            p.edit().putString("extra_ratios", val).apply();
                            refreshSummaries();
                        })
                        .setNegativeButton("Cancel", null)
                        .show();
                return true;
            default:
                return false;
        }
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
