// Copyright (C) 2026 IRedDragonICY
// SPDX-License-Identifier: GPL-3.0-or-later

package com.android.camera.fragment.settings;

import androidx.preference.Preference;
import androidx.preference.PreferenceScreen;
import androidx.preference.f;

public abstract class b extends f implements Preference.d {
    public PreferenceScreen mPreferenceGroup;

    public void goToActivity(Class<?> cls, String fragmentName) {}

    public void onResume() {}

    public abstract void addCurrentPreferences();

    public abstract int getFragmentTitle();

    public abstract void registerPreferenceListener();
}
