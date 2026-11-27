#pragma once

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <cstdlib>
#include <filesystem>

namespace Slic3r {

// The CLI uses the same OpenGL thumbnail renderer as the desktop. A surfaceless
// display and a private pbuffer also work with Mesa's software driver.
class CliThumbnailContext {
public:
    CliThumbnailContext() = default;
    CliThumbnailContext(const CliThumbnailContext &) = delete;
    CliThumbnailContext &operator=(const CliThumbnailContext &) = delete;

    ~CliThumbnailContext()
    {
        if (m_display != EGL_NO_DISPLAY) {
            if (m_context != EGL_NO_CONTEXT) {
                if (eglGetCurrentContext() == m_context)
                    eglMakeCurrent(m_display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
                eglDestroyContext(m_display, m_context);
            }
            if (m_surface != EGL_NO_SURFACE)
                eglDestroySurface(m_display, m_surface);
            eglTerminate(m_display);
        }
    }

    bool make_current()
    {
        if (m_display != EGL_NO_DISPLAY)
            return m_context != EGL_NO_CONTEXT && eglGetCurrentContext() == m_context;

        // The portable Linux distribution carries its own Mesa vendor and DRI
        // driver. A system install can use the platform's normal EGL discovery.
        std::error_code ec;
        auto lib = std::filesystem::read_symlink("/proc/self/exe", ec).parent_path().parent_path() / "lib";
        const auto vendor = lib / "egl_vendor.d" / "50_mesa.json";
        if (!ec && std::filesystem::is_regular_file(vendor, ec)) {
            setenv("__EGL_VENDOR_LIBRARY_FILENAMES", vendor.c_str(), 0);
            setenv("LIBGL_DRIVERS_PATH", (lib / "dri").c_str(), 0);
        }
        m_display = eglGetPlatformDisplay(EGL_PLATFORM_SURFACELESS_MESA, EGL_DEFAULT_DISPLAY, nullptr);
        if (m_display == EGL_NO_DISPLAY || !eglInitialize(m_display, nullptr, nullptr) || !eglBindAPI(EGL_OPENGL_API))
            return false;

        const EGLint attributes[] = {EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
                                     EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
        EGLConfig config;
        EGLint count = 0;
        if (!eglChooseConfig(m_display, attributes, &config, 1, &count) || count != 1)
            return false;
        const EGLint size[] = {EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE};
        m_surface = eglCreatePbufferSurface(m_display, config, size);
        m_context = eglCreateContext(m_display, config, EGL_NO_CONTEXT, nullptr);
        return m_surface != EGL_NO_SURFACE && m_context != EGL_NO_CONTEXT &&
               eglMakeCurrent(m_display, m_surface, m_surface, m_context);
    }

private:
    EGLDisplay m_display = EGL_NO_DISPLAY;
    EGLContext m_context = EGL_NO_CONTEXT;
    EGLSurface m_surface = EGL_NO_SURFACE;
};

} // namespace Slic3r
