// Hoofdprogramma van Local Transcribe for Iconik (Contents/MacOS).
// Laadt de meegeleverde Python in dit proces en start Resources/app/main.py, zodat
// Dock en Activity Monitor de naam en het icoon van de app tonen in plaats van python3.12.
#include <dlfcn.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int (*py_bytes_main_t)(int, char **);

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    char exe[PATH_MAX], resolved[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, resolved)) {
        fprintf(stderr, "launcher: eigen pad niet te bepalen\n");
        return 1;
    }
    char copy[PATH_MAX], macos_dir[PATH_MAX];
    snprintf(copy, sizeof(copy), "%s", resolved);      // dirname mag zijn invoer aanpassen
    snprintf(macos_dir, sizeof(macos_dir), "%s", dirname(copy));  // .../Contents/MacOS

    char home[PATH_MAX], lib[PATH_MAX], script[PATH_MAX];
    snprintf(home, sizeof(home), "%s/../Resources/python", macos_dir);
    snprintf(lib, sizeof(lib), "%s/lib/libpython3.12.dylib", home);
    snprintf(script, sizeof(script), "%s/../Resources/app/main.py", macos_dir);

    setenv("PYTHONHOME", home, 1);
    setenv("PYTHONNOUSERSITE", "1", 1);
    setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    setenv("PYTHONUNBUFFERED", "1", 1);

    void *handle = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    if (!handle) {
        fprintf(stderr, "launcher: libpython laden mislukt: %s\n", dlerror());
        return 1;
    }
    py_bytes_main_t run = (py_bytes_main_t)dlsym(handle, "Py_BytesMain");
    if (!run) {
        fprintf(stderr, "launcher: Py_BytesMain niet gevonden\n");
        return 1;
    }
    char *args[3] = { resolved, script, NULL };
    return run(2, args);
}
