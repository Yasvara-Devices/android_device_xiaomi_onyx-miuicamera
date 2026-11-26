#
# Copyright (C) 2026 IRedDragonICY
#
# SPDX-License-Identifier: GPL-3.0-or-later
#

# Inherit from the proprietary version
include vendor/xiaomi/onyx-miuicamera/BoardConfigVendor.mk

MIUICAMERA_PATH := device/xiaomi/onyx-miuicamera

# Properties
TARGET_SYSTEM_PROP += $(MIUICAMERA_PATH)/system.prop

# Sepolicy (includes MiuiCamera app + HAL + HEIC Codec2 tmpfs rules)
BOARD_VENDOR_SEPOLICY_DIRS += $(MIUICAMERA_PATH)/sepolicy/vendor
