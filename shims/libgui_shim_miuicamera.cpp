/*
 * Copyright (C) 2023 The LineageOS Project
 *
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

/*
 * MiuiCamera's JNI blobs (libcamera_algoup_jni.xiaomi.so and
 * libcamera_mianode_jni.xiaomi.so) import
 *
 *   android::lockImageFromBuffer(sp<GraphicBuffer>, uint32_t, Rect const&,
 *                                int, CpuConsumer::LockedBuffer*)
 *
 * On HyperOS this resolves against libandroid_runtime, which exports it from
 * frameworks/base/core/jni/android_media_Utils.cpp. It is not a public library,
 * so an app-namespace consumer cannot rely on it being reachable. Reimplement
 * it here instead, mirroring the AOSP behaviour.
 *
 * NOTE: this must NOT be declared extern "C" -- the blobs import the C++
 * mangled name, which requires normal C++ linkage inside namespace android.
 */

#include <gui/CpuConsumer.h>
#include <ui/GraphicBuffer.h>
#include <ui/Rect.h>
#include <utils/Errors.h>
#include <utils/Log.h>
#include <utils/StrongPointer.h>

namespace android {

namespace {

// Keep in sync with android_media_Utils.cpp. HAL_PIXEL_FORMAT_RAW_OPAQUE is
// deliberately absent -- CPU access to it is supported.
bool shimIsFormatOpaque(int format) {
    return format == HAL_PIXEL_FORMAT_IMPLEMENTATION_DEFINED;
}

// Blacklist, not whitelist: anything not known to be non-YUV is worth trying
// lockAsyncYCbCr on, which is what vendor-defined YUV formats rely on.
bool shimIsPossiblyYUV(PixelFormat format) {
    switch (static_cast<int>(format)) {
        case HAL_PIXEL_FORMAT_RGBA_8888:
        case HAL_PIXEL_FORMAT_RGBX_8888:
        case HAL_PIXEL_FORMAT_RGB_888:
        case HAL_PIXEL_FORMAT_RGB_565:
        case HAL_PIXEL_FORMAT_BGRA_8888:
        case HAL_PIXEL_FORMAT_Y8:
        case HAL_PIXEL_FORMAT_Y16:
        case HAL_PIXEL_FORMAT_RAW16:
        case HAL_PIXEL_FORMAT_RAW12:
        case HAL_PIXEL_FORMAT_RAW10:
        case HAL_PIXEL_FORMAT_RAW_OPAQUE:
        case HAL_PIXEL_FORMAT_BLOB:
        case HAL_PIXEL_FORMAT_IMPLEMENTATION_DEFINED:
        case HAL_PIXEL_FORMAT_YCBCR_P010:
            return false;

        case HAL_PIXEL_FORMAT_YV12:
        case HAL_PIXEL_FORMAT_YCbCr_420_888:
        case HAL_PIXEL_FORMAT_YCrCb_420_SP:
        default:
            return true;
    }
}

}  // namespace

status_t lockImageFromBuffer(sp<GraphicBuffer> buffer, uint32_t inUsage, const Rect& rect,
                             int fenceFd, CpuConsumer::LockedBuffer* outputImage) {
    if (buffer == nullptr || outputImage == nullptr) {
        ALOGE("Input GraphicBuffer or output LockedBuffer is NULL!");
        return BAD_VALUE;
    }

    int format = buffer->getPixelFormat();
    if (shimIsFormatOpaque(format)) {
        ALOGE("Opaque format buffer is not lockable!");
        return BAD_VALUE;
    }

    void* pData = nullptr;
    android_ycbcr ycbcr = android_ycbcr();
    status_t res;
    int flexFormat = format;

    if (shimIsPossiblyYUV(format)) {
        res = buffer->lockAsyncYCbCr(inUsage, rect, &ycbcr, fenceFd);
        if (res != OK) {
            ALOGW("lockAsyncYCbCr failed with error %d (format = 0x%x)", res, format);
        }

        pData = ycbcr.y;
        flexFormat = HAL_PIXEL_FORMAT_YCbCr_420_888;
    }

    // lockAsyncYCbCr for YUV was unsuccessful, or the format is not YUV at all.
    if (pData == nullptr) {
        res = buffer->lockAsync(inUsage, rect, &pData, fenceFd);
        if (res != OK) {
            ALOGE("Lock buffer failed!");
            return res;
        }
        // AOSP additionally unpacks P010/P210 via the gralloc4 plane layout here.
        // Those helpers are libandroid_runtime-internal; the generic mapping below
        // is what pre-gralloc4 callers got, and is what MiuiCamera exercises.
    }

    outputImage->data = reinterpret_cast<uint8_t*>(pData);
    outputImage->width = buffer->getWidth();
    outputImage->height = buffer->getHeight();
    outputImage->format = format;
    outputImage->flexFormat = flexFormat;
    outputImage->stride =
            (ycbcr.y != nullptr) ? static_cast<uint32_t>(ycbcr.ystride) : buffer->getStride();

    outputImage->dataCb = reinterpret_cast<uint8_t*>(ycbcr.cb);
    outputImage->dataCr = reinterpret_cast<uint8_t*>(ycbcr.cr);
    outputImage->chromaStride = static_cast<uint32_t>(ycbcr.cstride);
    outputImage->chromaStep = static_cast<uint32_t>(ycbcr.chroma_step);

    return OK;
}

}  // namespace android
