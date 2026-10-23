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
#include <unistd.h>

typedef int (*py_bytes_main_t)(int, char **);

int main(int argc, char **argv) {
    (void)argc;
    char exe[PATH_MAX], resolved[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, resolved)) {
        fprintf(stderr, "launcher: eigen pad niet te bepalen\n");
        return 1;
    }

    // macOS houdt vrijgegeven grote geheugenblokken in het proces vast voor hergebruik, in plaats van
    // ze terug te geven: na de audiostap van een lang bestand bleef zo 1,3 GB onnodig bezet. Met
    // MallocLargeCache=0 gaat het terug. De geheugenbeheerder leest dit bij de start van het proces,
    // dus een setenv hier komt te laat: zet hem en start dit programma één keer opnieuw (zelfde
    // proces-ID, nog voor Python of een venster bestaat). Gemeten 23 oktober 2026: piek 9,9 naar
    // 9,1 GB, tijd binnen een paar procent.
    if (!getenv("MallocLargeCache")) {
        setenv("MallocLargeCache", "0", 1);
        execv(resolved, argv);
        // Lukt dat niet, dan gewoon verder zonder.
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
