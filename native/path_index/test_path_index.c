/* Test for MosaicPathIndex, run under Wine. The exe sits in a fake game folder
 * (its folder is the game folder the index covers) next to the DLL:
 *   <root>/test_path_index.exe, <root>/Data/SKSE/Plugins/MosaicPathIndex.dll
 * It builds a Data/ with many entries, times lookups that miss, loads the
 * plugin, checks every behaviour and times the same lookups again. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdio.h>
#include <wchar.h>

static WCHAR root[MAX_PATH];
static int failures;

#define CHECK(cond, msg) do { if (cond) printf("ok   %s\n", msg); \
    else { printf("FAIL %s (err %lu)\n", msg, GetLastError()); failures++; } } while (0)

static void p(WCHAR *out, const WCHAR *rel) { swprintf(out, MAX_PATH, L"%ls\\%ls", root, rel); }

static double time_lookups(int n) {
    WCHAR a[MAX_PATH], b[MAX_PATH], c[MAX_PATH];
    p(a, L"data\\TEXTURES\\a.dds");          /* case mismatch, exists */
    p(b, L"Data\\data\\textures\\x.dds");    /* missing folder */
    p(c, L"Data\\missing.bsa");              /* missing file in the big folder */
    LARGE_INTEGER f, t0, t1;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t0);
    for (int i = 0; i < n; i++) {
        GetFileAttributesW(a);
        GetFileAttributesW(b);
        GetFileAttributesW(c);
    }
    QueryPerformanceCounter(&t1);
    return (double)(t1.QuadPart - t0.QuadPart) / f.QuadPart;
}

int main(void) {
    GetModuleFileNameW(NULL, root, MAX_PATH);
    *wcsrchr(root, L'\\') = 0;
    WCHAR path[MAX_PATH];

    p(path, L"Data\\Textures"); CreateDirectoryW(path, NULL);
    p(path, L"Data\\Textures\\a.dds");
    HANDLE h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL);
    DWORD w; WriteFile(h, "DDS!", 4, &w, NULL); CloseHandle(h);
    for (int i = 0; i < 5000; i++) {        /* a Data/ as big as a modlist's */
        swprintf(path, MAX_PATH, L"%ls\\Data\\Plugin %04d.esp", root, i);
        h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, 0, NULL);
        if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    }
    p(path, L"Data\\Mod.ini"); CloseHandle(CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL));

    double before = time_lookups(300);
    printf("without index: 900 lookups in %.3f s\n", before);

    p(path, L"Data\\SKSE\\Plugins\\MosaicPathIndex.dll");
    HMODULE m = LoadLibraryW(path);
    CHECK(m != NULL, "plugin loads");
    if (!m) return 1;
    typedef BOOLEAN (*load_t)(const void *);
    load_t load = (load_t)(void *)GetProcAddress(m, "SKSEPlugin_Load");
    CHECK(load && GetProcAddress(m, "SKSEPlugin_Version"), "SKSE exports present");
    load(NULL);

    double after = time_lookups(300);
    printf("with index:    900 lookups in %.3f s (%.0fx)\n", after, before / (after > 0 ? after : 1e-9));

    if (GetEnvironmentVariableW(L"MOSAIC_TEST_AFTER_LIST", NULL, 0)) {
        /* written to disk after Mosaic's file list: the changed folder must be
         * listed again, or this would wrongly be "not found" */
        p(path, L"data\\textures\\AFTER_LIST.DDS");
        CHECK(GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES, "a file added after the list is found");
        p(path, L"DATA\\NEW AFTER LIST\\deep\\X.TXT");
        CHECK(GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES, "a folder added after the list is indexed");
    }
    p(path, L"data\\TEXTURES\\A.DDS");
    CHECK(GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES, "case-mismatched file is found");
    h = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    char buf[8] = {0}; DWORD r = 0;
    if (h != INVALID_HANDLE_VALUE) { ReadFile(h, buf, 4, &r, NULL); CloseHandle(h); }
    CHECK(r == 4 && !memcmp(buf, "DDS!", 4), "case-mismatched file opens with its content");

    p(path, L"Data\\Textures\\nope.dds");
    SetLastError(0);
    CHECK(GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_FILE_NOT_FOUND,
          "missing file: ERROR_FILE_NOT_FOUND");
    p(path, L"Data\\data\\Textures\\nope.dds");
    SetLastError(0);
    CHECK(GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_PATH_NOT_FOUND,
          "missing folder: ERROR_PATH_NOT_FOUND");
    h = CreateFileW(path, GENERIC_READ, 0, NULL, OPEN_EXISTING, 0, NULL);
    CHECK(h == INVALID_HANDLE_VALUE && GetLastError() == ERROR_PATH_NOT_FOUND,
          "opening under a missing folder fails the same way");

    p(path, L"DATA\\textures\\new file.txt");
    h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, 0, NULL);
    CHECK(h != INVALID_HANDLE_VALUE, "creating a new file works");
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);
    p(path, L"Data\\Textures\\NEW FILE.TXT");
    CHECK(GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES, "the new file is found afterwards");

    p(path, L"data\\NewFolder");
    CHECK(CreateDirectoryW(path, NULL), "creating a folder works");
    p(path, L"Data\\newfolder\\inside.txt");
    h = CreateFileW(path, GENERIC_WRITE, 0, NULL, CREATE_NEW, 0, NULL);
    CHECK(h != INVALID_HANDLE_VALUE, "creating a file inside the new folder works");
    if (h != INVALID_HANDLE_VALUE) CloseHandle(h);

    WIN32_FIND_DATAW fd;
    p(path, L"data\\textures\\*");
    int n = 0;
    h = FindFirstFileW(path, &fd);
    if (h != INVALID_HANDLE_VALUE) { do n++; while (FindNextFileW(h, &fd)); FindClose(h); }
    int expect = GetEnvironmentVariableW(L"MOSAIC_TEST_AFTER_LIST", NULL, 0) ? 5 : 4;
    CHECK(n == expect, "listing a case-mismatched folder works (., .., its files)");

    p(path, L"Data\\mod.INI");
    WIN32_FILE_ATTRIBUTE_DATA ad;
    CHECK(GetFileAttributesExW(path, GetFileExInfoStandard, &ad), "GetFileAttributesEx with case mismatch");

    WCHAR from[MAX_PATH], to[MAX_PATH];
    p(from, L"Data\\Mod.ini"); p(to, L"Data\\Renamed.ini");
    CHECK(MoveFileW(from, to), "renaming works");
    CHECK(GetFileAttributesW(to) != INVALID_FILE_ATTRIBUTES, "the renamed file is found afterwards");
    p(path, L"data\\RENAMED.INI");
    CHECK(GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES, "the renamed file is found with another case");

    WCHAR tmp[MAX_PATH], out[MAX_PATH];
    GetTempPathW(MAX_PATH, tmp);
    swprintf(out, MAX_PATH, L"%lsmosaic-pathindex-save.tmp", tmp);
    swprintf(from, MAX_PATH, L"%lsmosaic-pathindex-save.ess", tmp);
    DeleteFileW(from);
    CloseHandle(CreateFileW(out, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, 0, NULL));
    CHECK(MoveFileW(out, from), "a rename outside the game folder (a save) works");
    DeleteFileW(from);

    p(from, L"Data\\NewFolder"); p(to, L"Data\\Moved Folder");
    CHECK(MoveFileW(from, to), "renaming a folder works");
    p(path, L"data\\moved folder\\INSIDE.TXT");
    CHECK(GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES, "files inside a renamed folder are found");
    Sleep(1000);                              /* the folder's contents get indexed */
    p(path, L"DATA\\MOVED FOLDER\\inside.TXT");
    CHECK(GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES, "...and later from the index, any case");
    p(path, L"Data\\Moved Folder\\nope.txt");
    SetLastError(0);
    CHECK(GetFileAttributesW(path) == INVALID_FILE_ATTRIBUTES && GetLastError() == ERROR_FILE_NOT_FOUND,
          "...and a missing file in it is not found");

    double later = time_lookups(300);
    printf("after renames: 900 lookups in %.3f s\n", later);
    CHECK(later < before / 10, "misses are still answered after those renames");

    printf(failures ? "\n%d FAILED\n" : "\nall passed\n", failures);
    return failures != 0;
}
