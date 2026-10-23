// Original PNUT WebView2 host adapter. All calls run on the Qt GUI/COM STA thread.
#pragma once
#include <stdint.h>
#include <stddef.h>

#ifdef _WIN32
#define PNUT_ATLAS_API __declspec(dllexport)
#define PNUT_ATLAS_CALL __cdecl
#else
#define PNUT_ATLAS_API
#define PNUT_ATLAS_CALL
#endif

#ifdef __cplusplus
extern "C" {
#endif

enum PnutAtlasEvent {
    PNUT_ATLAS_READY = 1,
    PNUT_ATLAS_LOAD_START = 2,       // data=URL
    PNUT_ATLAS_LOAD_DONE = 3,        // value=1 success, 0 failure; data=error description
    PNUT_ATLAS_URL_CHANGED = 4,      // data=actual source, including history.push/replaceState
    PNUT_ATLAS_NAV_REQUEST = 5,      // value bits: 1=user initiated, 2=redirect; data=URL
    PNUT_ATLAS_NEW_WINDOW = 6,       // value bit 1=user initiated; data=URL; native popup suppressed
    PNUT_ATLAS_PROCESS_FAILED = 7,   // value=COREWEBVIEW2_PROCESS_FAILED_KIND
    PNUT_ATLAS_JS_RESULT = 8,        // value=request id; data=WebView2 result JSON
    PNUT_ATLAS_ERROR = 9            // value=HRESULT; data=description (no profile paths)
};

// Strings are UTF-16, valid only until the callback returns. Copy them in Python.
// Only NAV_REQUEST uses the return value: exactly 1 allows navigation; all other
// values cancel. No callback or native function may throw across this C boundary.
typedef int (PNUT_ATLAS_CALL *PnutAtlasCallback)(void *context, int event, int value,
                                               const wchar_t *data);

// hwnd must belong to a dedicated PNUT child host; close before destroying it.
// userdata is a dedicated absolute directory, shared by PNUT's Atlas views.
// create returns before asynchronous READY/ERROR; NULL indicates immediate error.
PNUT_ATLAS_API void *PNUT_ATLAS_CALL pnut_atlas_create(void *hwnd, const wchar_t *userdata,
                                                     PnutAtlasCallback callback, void *context);
// Idempotence is the caller's responsibility: clear the handle after close.
// close disables every callback before removing handlers/closing the controller.
PNUT_ATLAS_API void PNUT_ATLAS_CALL pnut_atlas_close(void *handle);

// These return an HRESULT (signed int32). S_FALSE means not ready/unsupported.
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_navigate(void *handle, const wchar_t *url);
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_load_html(void *handle, const wchar_t *html);
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_eval_js(void *handle, const wchar_t *script,
                                                      int request_id);
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_resize(void *handle);
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_stop(void *handle);
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_set_visible(void *handle, int visible);
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_suspend(void *handle);
PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_resume(void *handle);
// Required wchar count including NUL; zero means runtime unavailable. No COM/app
// initialization or browser process is needed. A short buffer receives no text.
PNUT_ATLAS_API int PNUT_ATLAS_CALL pnut_atlas_runtime_version(wchar_t *buffer, int capacity);

#ifdef __cplusplus
}
#endif
