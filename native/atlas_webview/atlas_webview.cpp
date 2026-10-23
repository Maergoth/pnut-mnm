// Original PNUT adapter around Microsoft's installed Evergreen WebView2 Runtime.
// No game APIs, host objects, web-message bridge, synthetic input or DLL injection.
#include "atlas_webview.h"
#include <windows.h>
#include <objbase.h>
#include <wrl.h>
#include <WebView2.h>
#include <WebView2EnvironmentOptions.h>
#include <memory>
#include <new>
#include <string>
#include <cwchar>
#include <climits>

using Microsoft::WRL::Callback;
using Microsoft::WRL::ComPtr;
using Microsoft::WRL::Make;

namespace {
struct CoMemDeleter {
    void operator()(wchar_t *p) const noexcept { CoTaskMemFree(p); }
};
using CoString = std::unique_ptr<wchar_t, CoMemDeleter>;

template <typename Function> HRESULT guarded(Function &&function) noexcept {
    try { return function(); }
    catch (const std::bad_alloc &) { return E_OUTOFMEMORY; }
    catch (...) { return E_FAIL; }
}

struct State : std::enable_shared_from_this<State> {
    HWND host;
    DWORD thread;
    std::wstring userdata;
    PnutAtlasCallback callback;
    void *context;
    bool closed = false;
    bool visible = true;
    bool com_initialized = false;
    HRESULT apartment_result = E_FAIL;
    std::wstring pending_url;
    std::wstring pending_html;
    ComPtr<ICoreWebView2Environment> environment;
    ComPtr<ICoreWebView2Controller> controller;
    ComPtr<ICoreWebView2> webview;
    ComPtr<ICoreWebView2_4> webview4;
    EventRegistrationToken navigation_token{}, completed_token{}, source_token{}, history_token{};
    EventRegistrationToken window_token{}, permission_token{}, download_token{}, failed_token{};
    bool navigation_added = false, completed_added = false, source_added = false, history_added = false;
    bool window_added = false, permission_added = false, download_added = false, failed_added = false;
    UINT64 latest_navigation = 0;

    State(HWND window, const wchar_t *folder, PnutAtlasCallback fn, void *ctx)
        : host(window), thread(GetCurrentThreadId()), userdata(folder), callback(fn), context(ctx) {
        apartment_result = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
        com_initialized = SUCCEEDED(apartment_result);
    }

    ~State() {
        shutdown();
        if (com_initialized) CoUninitialize();
    }

    int emit(int event, int value = 0, const wchar_t *data = L"") noexcept {
        if (closed || !callback) return 0;
        try { return callback(context, event, value, data ? data : L""); }
        catch (...) { return 0; }
    }

    HRESULT fail(HRESULT result, const wchar_t *message) noexcept {
        emit(PNUT_ATLAS_ERROR, static_cast<int>(result), message);
        return result;
    }

    HRESULT check_thread() const noexcept {
        if (GetCurrentThreadId() != thread) return RPC_E_WRONG_THREAD;
        return closed ? RO_E_CLOSED : S_OK;
    }

    void shutdown() noexcept {
        if (closed) return;
        // Python may now release its CFUNCTYPE/context immediately. Pending COM
        // completions retain only this shared state and never call Python again.
        closed = true;
        callback = nullptr;
        context = nullptr;
        if (webview) {
            // Removing a handler can fail after a crashed runtime; still release
            // every remaining handler/controller. No callback is enabled here.
            if (navigation_added) (void)webview->remove_NavigationStarting(navigation_token);
            if (completed_added) (void)webview->remove_NavigationCompleted(completed_token);
            if (source_added) (void)webview->remove_SourceChanged(source_token);
            if (history_added) (void)webview->remove_HistoryChanged(history_token);
            if (window_added) (void)webview->remove_NewWindowRequested(window_token);
            if (permission_added) (void)webview->remove_PermissionRequested(permission_token);
            if (failed_added) (void)webview->remove_ProcessFailed(failed_token);
            if (download_added && webview4) (void)webview4->remove_DownloadStarting(download_token);
            (void)webview->Stop();
        }
        if (controller) (void)controller->Close();
        webview4.Reset();
        webview.Reset();
        controller.Reset();
        environment.Reset();
    }

    HRESULT resize() noexcept {
        if (!controller) return S_FALSE;
        RECT bounds{};
        if (!GetClientRect(host, &bounds)) return HRESULT_FROM_WIN32(GetLastError());
        return controller->put_Bounds(bounds);
    }

    HRESULT source_changed() noexcept {
        if (closed || !webview) return S_OK;
        wchar_t *raw = nullptr;
        const HRESULT hr = webview->get_Source(&raw);
        CoString text(raw);
        if (FAILED(hr)) return fail(hr, L"Atlas could not read its current page address.");
        emit(PNUT_ATLAS_URL_CHANGED, 0, text.get());
        return S_OK;
    }

    HRESULT configure() {
        auto self = shared_from_this();
        ComPtr<ICoreWebView2Settings> settings;
        HRESULT hr = webview->get_Settings(&settings);
        if (FAILED(hr)) return fail(hr, L"Atlas browser settings are unavailable.");
#define REQUIRE_SETTING(expression) do { hr = (expression); if (FAILED(hr)) return fail(hr, L"Atlas browser settings could not be applied."); } while (0)
        REQUIRE_SETTING(settings->put_IsScriptEnabled(TRUE));
        REQUIRE_SETTING(settings->put_IsWebMessageEnabled(FALSE));
        REQUIRE_SETTING(settings->put_AreHostObjectsAllowed(FALSE));
        REQUIRE_SETTING(settings->put_AreDevToolsEnabled(FALSE));
        REQUIRE_SETTING(settings->put_IsStatusBarEnabled(FALSE));
        REQUIRE_SETTING(settings->put_IsBuiltInErrorPageEnabled(FALSE));
#undef REQUIRE_SETTING

        hr = webview->add_NavigationStarting(Callback<ICoreWebView2NavigationStartingEventHandler>(
            [self](ICoreWebView2 *, ICoreWebView2NavigationStartingEventArgs *args) -> HRESULT {
                return guarded([&]() -> HRESULT {
                    // Fail closed even if metadata retrieval, allocation, or the
                    // Python policy callback fails before making its decision.
                    HRESULT result = args->put_Cancel(TRUE);
                    if (FAILED(result)) {
                        self->fail(result, L"Atlas navigation could not be blocked safely.");
                        self->shutdown();
                        return result;
                    }
                    if (self->closed) return S_OK;
                    wchar_t *raw = nullptr;
                    result = args->get_Uri(&raw);
                    CoString url(raw);
                    if (FAILED(result)) return self->fail(result, L"Atlas navigation could not be checked.");
                    BOOL user = FALSE, redirect = FALSE;
                    result = args->get_IsUserInitiated(&user);
                    if (FAILED(result)) return result;
                    result = args->get_IsRedirected(&redirect);
                    if (FAILED(result)) return result;
                    const int allow = self->emit(PNUT_ATLAS_NAV_REQUEST, (user ? 1 : 0) | (redirect ? 2 : 0), url.get());
                    if (self->closed || allow != 1) return S_OK;
                    result = args->get_NavigationId(&self->latest_navigation);
                    if (FAILED(result)) return result;
                    result = args->put_Cancel(FALSE);
                    if (FAILED(result)) return self->fail(result, L"Atlas navigation could not be allowed safely.");
                    self->emit(PNUT_ATLAS_LOAD_START, 0, url.get());
                    return S_OK;
                });
            }).Get(), &navigation_token);
        if (FAILED(hr)) return fail(hr, L"Atlas navigation controls are unavailable.");
        navigation_added = true;

        hr = webview->add_NavigationCompleted(Callback<ICoreWebView2NavigationCompletedEventHandler>(
            [self](ICoreWebView2 *, ICoreWebView2NavigationCompletedEventArgs *args) -> HRESULT {
                return guarded([&]() -> HRESULT {
                    if (self->closed) return S_OK;
                    UINT64 id = 0;
                    HRESULT result = args->get_NavigationId(&id);
                    if (FAILED(result)) return result;
                    if (id != self->latest_navigation) return S_OK;
                    BOOL success = FALSE;
                    result = args->get_IsSuccess(&success);
                    if (FAILED(result)) return self->fail(result, L"Atlas loading state is unavailable.");
                    COREWEBVIEW2_WEB_ERROR_STATUS status = COREWEBVIEW2_WEB_ERROR_STATUS_UNKNOWN;
                    result = args->get_WebErrorStatus(&status);
                    if (FAILED(result)) return result;
                    // Canceled old requests and rejected links must not replace
                    // a valid map or a newer pending navigation with an error.
                    if (!success && status == COREWEBVIEW2_WEB_ERROR_STATUS_OPERATION_CANCELED) return S_OK;
                    if (success) {
                        result = self->source_changed();
                        if (FAILED(result) || self->closed) return result;
                    }
                    self->emit(PNUT_ATLAS_LOAD_DONE, success ? 1 : 0,
                               success ? L"" : L"Atlas could not load. Check your connection or use saved maps.");
                    return S_OK;
                });
            }).Get(), &completed_token);
        if (FAILED(hr)) return fail(hr, L"Atlas loading notifications are unavailable.");
        completed_added = true;

        hr = webview->add_SourceChanged(Callback<ICoreWebView2SourceChangedEventHandler>(
            [self](ICoreWebView2 *, ICoreWebView2SourceChangedEventArgs *) -> HRESULT {
                return self->source_changed();
            }).Get(), &source_token);
        if (FAILED(hr)) return fail(hr, L"Atlas address notifications are unavailable.");
        source_added = true;
        hr = webview->add_HistoryChanged(Callback<ICoreWebView2HistoryChangedEventHandler>(
            [self](ICoreWebView2 *, IUnknown *) -> HRESULT { return self->source_changed(); }
            ).Get(), &history_token);
        if (FAILED(hr)) return fail(hr, L"Atlas history notifications are unavailable.");
        history_added = true;

        hr = webview->add_NewWindowRequested(Callback<ICoreWebView2NewWindowRequestedEventHandler>(
            [self](ICoreWebView2 *, ICoreWebView2NewWindowRequestedEventArgs *args) -> HRESULT {
                return guarded([&]() -> HRESULT {
                    HRESULT result = args->put_Handled(TRUE);
                    if (FAILED(result)) {
                        self->fail(result, L"Atlas popup could not be blocked safely.");
                        self->shutdown();
                        return result;
                    }
                    if (self->closed) return S_OK;
                    wchar_t *raw = nullptr;
                    result = args->get_Uri(&raw);
                    CoString url(raw);
                    if (FAILED(result)) return result;
                    BOOL user = FALSE;
                    result = args->get_IsUserInitiated(&user);
                    if (FAILED(result)) return result;
                    self->emit(PNUT_ATLAS_NEW_WINDOW, user ? 1 : 0, url.get());
                    return S_OK;
                });
            }).Get(), &window_token);
        if (FAILED(hr)) return fail(hr, L"Atlas external-link controls are unavailable.");
        window_added = true;

        hr = webview->add_PermissionRequested(Callback<ICoreWebView2PermissionRequestedEventHandler>(
            [self](ICoreWebView2 *, ICoreWebView2PermissionRequestedEventArgs *args) -> HRESULT {
                const HRESULT result = args->put_State(COREWEBVIEW2_PERMISSION_STATE_DENY);
                if (FAILED(result)) {
                    self->fail(result, L"Atlas permissions could not be denied safely.");
                    self->shutdown();
                }
                return result;
            }).Get(), &permission_token);
        if (FAILED(hr)) return fail(hr, L"Atlas permission controls are unavailable.");
        permission_added = true;

        hr = webview.As(&webview4);
        if (FAILED(hr)) return fail(hr, L"Update Microsoft Edge WebView2 Runtime to open Atlas.");
        hr = webview4->add_DownloadStarting(Callback<ICoreWebView2DownloadStartingEventHandler>(
            [self](ICoreWebView2 *, ICoreWebView2DownloadStartingEventArgs *args) -> HRESULT {
                HRESULT result = args->put_Cancel(TRUE);
                if (FAILED(result)) {
                    self->fail(result, L"Atlas download could not be blocked safely.");
                    self->shutdown();
                    return result;
                }
                return args->put_Handled(TRUE);
            }).Get(), &download_token);
        if (FAILED(hr)) return fail(hr, L"Atlas download controls are unavailable.");
        download_added = true;

        hr = webview->add_ProcessFailed(Callback<ICoreWebView2ProcessFailedEventHandler>(
            [self](ICoreWebView2 *, ICoreWebView2ProcessFailedEventArgs *args) -> HRESULT {
                if (self->closed) return S_OK;
                COREWEBVIEW2_PROCESS_FAILED_KIND kind;
                const HRESULT result = args->get_ProcessFailedKind(&kind);
                if (FAILED(result)) return result;
                self->emit(PNUT_ATLAS_PROCESS_FAILED, static_cast<int>(kind), L"Atlas stopped unexpectedly. Retry or use saved maps.");
                return S_OK;
            }).Get(), &failed_token);
        if (FAILED(hr)) return fail(hr, L"Atlas process notifications are unavailable.");
        failed_added = true;
        hr = resize();
        if (FAILED(hr)) return fail(hr, L"Atlas could not fit its map window.");
        hr = controller->put_IsVisible(visible ? TRUE : FALSE);
        if (FAILED(hr)) return fail(hr, L"Atlas map visibility could not be set.");
        emit(PNUT_ATLAS_READY);
        if (closed) return S_OK;
        // READY can synchronously trigger Python navigation; consume an older
        // pre-ready request only when Python did not replace it in its callback.
        if (!pending_url.empty()) {
            const std::wstring url = std::move(pending_url);
            pending_html.clear();
            return webview->Navigate(url.c_str());
        }
        if (!pending_html.empty()) {
            const std::wstring html = std::move(pending_html);
            return webview->NavigateToString(html.c_str());
        }
        return S_OK;
    }

    HRESULT initialize() {
        if (FAILED(apartment_result)) return fail(apartment_result, L"Atlas requires the application's main GUI thread.");
        auto self = shared_from_this();
        auto options = Make<CoreWebView2EnvironmentOptions>();
        if (!options) return E_OUTOFMEMORY;
        HRESULT hr = options->put_AdditionalBrowserArguments(L"--disk-cache-size=67108864");
        if (FAILED(hr)) return fail(hr, L"Atlas cache settings could not be applied.");
        hr = options->put_AllowSingleSignOnUsingOSPrimaryAccount(FALSE);
        if (FAILED(hr)) return fail(hr, L"Atlas browser isolation could not be configured.");
        hr = options->put_ReleaseChannels(COREWEBVIEW2_RELEASE_CHANNELS_STABLE);
        if (FAILED(hr)) return fail(hr, L"Atlas could not select the stable Microsoft WebView2 Runtime.");
        return CreateCoreWebView2EnvironmentWithOptions(nullptr, userdata.c_str(), options.Get(),
            Callback<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler>(
                [self](HRESULT result, ICoreWebView2Environment *env) -> HRESULT {
                    return guarded([&]() -> HRESULT {
                        if (self->closed) return S_OK;
                        if (FAILED(result) || !env) return self->fail(FAILED(result) ? result : E_POINTER,
                            L"Microsoft Edge WebView2 Runtime could not start. Install or repair the runtime, or use saved maps.");
                        if (!IsWindow(self->host)) return self->fail(E_HANDLE, L"Atlas map window is no longer available.");
                        self->environment = env;
                        const HRESULT hr = env->CreateCoreWebView2Controller(self->host,
                            Callback<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler>(
                                [self](HRESULT status, ICoreWebView2Controller *controller) -> HRESULT {
                                    return guarded([&]() -> HRESULT {
                                        if (self->closed) {
                                            if (controller) (void)controller->Close();
                                            return S_OK;
                                        }
                                        if (FAILED(status) || !controller) return self->fail(FAILED(status) ? status : E_POINTER,
                                            L"Atlas browser window could not be created. Retry or use saved maps.");
                                        self->controller = controller;
                                        HRESULT result = controller->get_CoreWebView2(&self->webview);
                                        if (FAILED(result) || !self->webview) return self->fail(FAILED(result) ? result : E_POINTER,
                                            L"Atlas browser is unavailable. Retry or use saved maps.");
                                        result = self->configure();
                                        if (FAILED(result)) self->shutdown();
                                        return result;
                                    });
                                }).Get());
                        if (FAILED(hr)) return self->fail(hr, L"Atlas browser window could not start.");
                        return S_OK;
                    });
                }).Get());
    }
};

struct Handle { std::shared_ptr<State> state; };

template <typename Function> int32_t with_state(void *handle, Function &&function) noexcept {
    if (!handle) return E_HANDLE;
    return guarded([&]() -> HRESULT {
        auto state = static_cast<Handle *>(handle)->state;
        const HRESULT result = state->check_thread();
        if (FAILED(result)) return result;
        return function(state);
    });
}
} // namespace

extern "C" PNUT_ATLAS_API void *PNUT_ATLAS_CALL pnut_atlas_create(
        void *hwnd, const wchar_t *userdata, PnutAtlasCallback callback, void *context) {
    if (!hwnd || !IsWindow(static_cast<HWND>(hwnd)) || !userdata || !*userdata || !callback) return nullptr;
    try {
        auto handle = std::make_unique<Handle>();
        handle->state = std::make_shared<State>(static_cast<HWND>(hwnd), userdata, callback, context);
        const HRESULT result = handle->state->initialize();
        if (FAILED(result)) {
            handle->state->fail(result, L"Atlas could not start. Install or repair Microsoft Edge WebView2 Runtime, or use saved maps.");
            handle->state->shutdown();
            return nullptr;
        }
        return handle.release();
    } catch (...) { return nullptr; }
}

extern "C" PNUT_ATLAS_API void PNUT_ATLAS_CALL pnut_atlas_close(void *handle) {
    if (!handle) return;
    try {
        std::unique_ptr<Handle> holder(static_cast<Handle *>(handle));
        holder->state->shutdown();
    } catch (...) {}
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_navigate(void *handle, const wchar_t *url) {
    if (!url || !*url) return E_INVALIDARG;
    return with_state(handle, [&](const std::shared_ptr<State> &state) -> HRESULT {
        state->pending_html.clear();
        if (!state->webview) { state->pending_url = url; return S_FALSE; }
        state->pending_url.clear();
        return state->webview->Navigate(url);
    });
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_load_html(void *handle, const wchar_t *html) {
    if (!html) return E_INVALIDARG;
    return with_state(handle, [&](const std::shared_ptr<State> &state) -> HRESULT {
        state->pending_url.clear();
        if (!state->webview) { state->pending_html = html; return S_FALSE; }
        state->pending_html.clear();
        return state->webview->NavigateToString(html);
    });
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_eval_js(void *handle, const wchar_t *script, int request_id) {
    if (!script) return E_INVALIDARG;
    return with_state(handle, [&](const std::shared_ptr<State> &state) -> HRESULT {
        if (!state->webview) return S_FALSE;
        return state->webview->ExecuteScript(script, Callback<ICoreWebView2ExecuteScriptCompletedHandler>(
            [state, request_id](HRESULT result, LPCWSTR json) -> HRESULT {
                if (!state->closed) state->emit(PNUT_ATLAS_JS_RESULT, request_id, SUCCEEDED(result) && json ? json : L"null");
                return S_OK;
            }).Get());
    });
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_resize(void *handle) {
    return with_state(handle, [](const std::shared_ptr<State> &state) { return state->resize(); });
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_stop(void *handle) {
    return with_state(handle, [](const std::shared_ptr<State> &state) -> HRESULT {
        return state->webview ? state->webview->Stop() : S_FALSE;
    });
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_set_visible(void *handle, int visible) {
    return with_state(handle, [&](const std::shared_ptr<State> &state) -> HRESULT {
        state->visible = visible != 0;
        return state->controller ? state->controller->put_IsVisible(state->visible ? TRUE : FALSE) : S_FALSE;
    });
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_suspend(void *handle) {
    return with_state(handle, [](const std::shared_ptr<State> &state) -> HRESULT {
        if (!state->webview) return S_FALSE;
        ComPtr<ICoreWebView2_3> view;
        const HRESULT result = state->webview.As(&view);
        if (FAILED(result)) return result == E_NOINTERFACE ? S_FALSE : result;
        return view->TrySuspend(Callback<ICoreWebView2TrySuspendCompletedHandler>(
            [state](HRESULT result, BOOL) -> HRESULT {
                if (FAILED(result) && !state->closed) state->fail(result, L"Atlas could not pause its hidden map.");
                return S_OK;
            }).Get());
    });
}

extern "C" PNUT_ATLAS_API int32_t PNUT_ATLAS_CALL pnut_atlas_resume(void *handle) {
    return with_state(handle, [](const std::shared_ptr<State> &state) -> HRESULT {
        if (!state->webview) return S_FALSE;
        ComPtr<ICoreWebView2_3> view;
        const HRESULT result = state->webview.As(&view);
        if (FAILED(result)) return result == E_NOINTERFACE ? S_FALSE : result;
        return view->Resume();
    });
}

extern "C" PNUT_ATLAS_API int PNUT_ATLAS_CALL pnut_atlas_runtime_version(wchar_t *buffer, int capacity) {
    try {
        auto options = Make<CoreWebView2EnvironmentOptions>();
        if (!options || FAILED(options->put_ReleaseChannels(COREWEBVIEW2_RELEASE_CHANNELS_STABLE))) return 0;
        wchar_t *raw = nullptr;
        const HRESULT result = GetAvailableCoreWebView2BrowserVersionStringWithOptions(nullptr, options.Get(), &raw);
        CoString version(raw);
        if (FAILED(result) || !raw) return 0;
        const size_t required = std::wcslen(raw) + 1;
        if (required > INT_MAX) return 0;
        if (buffer && capacity >= static_cast<int>(required)) std::wmemcpy(buffer, raw, required);
        return static_cast<int>(required);
    } catch (...) { return 0; }
}
