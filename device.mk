#
# Copyright (C) 2026 IRedDragonICY
#
# SPDX-License-Identifier: GPL-3.0-or-later
#

# Inherit from the proprietary version
$(call inherit-product, vendor/xiaomi/onyx-miuicamera/onyx-miuicamera-vendor.mk)

# Camera
$(call soong_config_set_bool,camera,override_format_from_reserved,true)

# Framework allowlists
#
# This has to land in /product, not /system: init loads /vendor/build.prop after
# /system/build.prop, and the stock vendor image already sets this key, so a
# value in system.prop is silently overridden. /product/etc/build.prop is loaded
# last and wins.
#
# Not a ro. prop, so the value is capped at 92 bytes - keep this list short.
PRODUCT_PRODUCT_PROPERTIES += \
    persist.vendor.camera.privapp.list=com.android.camera,com.agc.gcam,com.google.android.GoogleCamera

# Public libraries
PRODUCT_COPY_FILES += \
    $(LOCAL_PATH)/configs/public.libraries-xiaomi.txt:$(TARGET_COPY_OUT_SYSTEM)/etc/public.libraries-xiaomi.txt

# Priv-app permission
PRODUCT_COPY_FILES += \
    $(LOCAL_PATH)/configs/privapp-permissions-miuicamera.xml:$(TARGET_COPY_OUT_SYSTEM)/etc/permissions/privapp-permissions-miuicamera.xml

# Soong namespaces
PRODUCT_SOONG_NAMESPACES += \
    $(LOCAL_PATH)

# Sysconfig
PRODUCT_COPY_FILES += \
    $(LOCAL_PATH)/configs/miuicamera-hiddenapi-package-allowlist.xml:$(TARGET_COPY_OUT_SYSTEM)/etc/sysconfig/miuicamera-hiddenapi-package-allowlist.xml
