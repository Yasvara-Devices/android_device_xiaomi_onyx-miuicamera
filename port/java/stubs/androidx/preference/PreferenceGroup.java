// Copyright (C) 2026 IRedDragonICY
// SPDX-License-Identifier: GPL-3.0-or-later

package androidx.preference;

import android.content.Context;
import android.util.AttributeSet;

public abstract class PreferenceGroup extends Preference {
    public PreferenceGroup(Context context, AttributeSet attrs) {
        super(context, attrs);
    }

    public boolean __SYM_ADD_PREF__(Preference pref) { return false; }
    public Preference __SYM_FIND_PREF__(CharSequence key) { return null; }
}
