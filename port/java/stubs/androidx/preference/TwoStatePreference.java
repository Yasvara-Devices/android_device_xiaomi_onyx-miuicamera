// Copyright (C) 2026 IRedDragonICY
// SPDX-License-Identifier: GPL-3.0-or-later

package androidx.preference;

import android.content.Context;
import android.util.AttributeSet;

public abstract class TwoStatePreference extends Preference {
    public TwoStatePreference(Context context, AttributeSet attrs) {
        super(context, attrs);
    }

    public void setChecked(boolean checked) {}
}
