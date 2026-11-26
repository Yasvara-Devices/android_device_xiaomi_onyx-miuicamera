// Copyright (C) 2026 IRedDragonICY
// SPDX-License-Identifier: GPL-3.0-or-later

package androidx.preference;

import android.content.Context;
import android.util.AttributeSet;

public class Preference {
    public interface d {
        boolean onPreferenceClick(Preference pref);
    }

    public String __SYM_KEY__;
    public boolean __SYM_PERSISTENT__;
    public d __SYM_ONCLICK__;

    public Preference(Context context, AttributeSet attrs) {}

    public void __SYM_SET_KEY__(String key) {}
    public void __SYM_SET_TITLE__(String title) {}
    public void __SYM_SET_SUMMARY__(CharSequence summary) {}
}
