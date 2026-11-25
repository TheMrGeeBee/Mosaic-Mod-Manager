/*
 * MosaicPathIndex -- a file-lookup index for Bethesda games under Wine/Proton.
 *
 * Under Wine every lookup whose exact path misses (a different case, or a file
 * that simply isn't there) makes Wine read the whole folder and compare every
 * entry, computing 8.3 short names as it goes. A modded Data/ folder holds
 * thousands of entries, and the game makes hundreds of thousands of such
 * lookups while it loads: LoreRim spent ~150 s of a 360 s load on this.
 *
 * MO2 never pays for it because its USVFS answers lookups from its own tree.
 * This DLL does the small part of that which matters here: it indexes the game
 * folder once, case-insensitively, and in front of the ntdll file calls
 *   - rewrites a path to its real casing, so Wine's exact lookup hits, and
 *   - answers "not found" for paths the index doesn't have, without Wine.
 * Calls that may create a file pass through and add it to the index; a rename
 * anywhere switches the "not found" answers off for the rest of the session.
 *
 * Loaded as a script-extender plugin: one source, built 64-bit (SKSE64,
 * SKSE VR, F4SE, F4SE VR, SFSE) and 32-bit (SKSE, NVSE, FOSE, OBSE), with
 * every extender's entry points exported. Extenders load plugins before the
 * slow part of the load. The DLL's log and Mosaic's file list sit next to the
 * DLL, in whichever <Extender>\Plugins folder Mosaic put it. Environment switches: MOSAIC_PATHINDEX=0 disables it entirely,
 * MOSAIC_PATHINDEX_NOFAIL=1 keeps the case fixing but never answers misses.
 *
 * Copyright (C) 2026 Mosaic Mod Manager contributors. GPL-3.0-or-later.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winternl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <wchar.h>
#include <stddef.h>

#ifndef STATUS_OBJECT_NAME_NOT_FOUND
#define STATUS_OBJECT_NAME_NOT_FOUND ((NTSTATUS)0xC0000034L)
#endif
#ifndef STATUS_OBJECT_PATH_NOT_FOUND
#define STATUS_OBJECT_PATH_NOT_FOUND ((NTSTATUS)0xC000003AL)
#endif
#define PI_FILE_SUPERSEDE 0
#define PI_FILE_OPEN 1
#define PI_FILE_OVERWRITE 4
#define PI_FILE_DIRECTORY_FILE 0x00000001
#define PI_FileRenameInformation 10
#define PI_FileLinkInformation 11
#define PI_FileRenameInformationEx 65
#define PI_FileLinkInformationEx 72

/* ------------------------------------------------------------------ SKSE */

typedef struct {
    uint32_t dataVersion;
    uint32_t pluginVersion;
    char name[256];
    char author[256];
    char supportEmail[252];
    uint32_t versionIndependenceEx;
    uint32_t versionIndependence;
    uint32_t compatibleVersions[16];
    uint32_t seVersionRequired;
} SKSEPluginVersionData;

__declspec(dllexport) SKSEPluginVersionData SKSEPlugin_Version = {
    1, 1, "MosaicPathIndex", "Mosaic Mod Manager", "",
    1 /* NoStructUse: touches no game structures */,
    1 /* AddressLibraryPostAE: uses no game addresses either */,
    {0}, 0,
};

/* ----------------------------------------------------------------- index */

typedef struct Dir Dir;
typedef struct Entry {
    struct Entry *next;
    uint32_t hash;
    uint16_t len;
    Dir *dir;            /* NULL for a file */
    WCHAR *exact;        /* name as on disk */
    WCHAR *upper;        /* upper-cased, for matching */
} Entry;
struct Dir {
    Entry **buckets;
    uint32_t nbuckets;
    uint32_t count;
    WCHAR *pending;      /* renamed into place, contents not indexed yet: its path */
    Dir *next_pending;
    uint64_t ft;         /* last-write time Mosaic's file list recorded (0: none) */
};

static HANDLE g_heap;
static Dir *g_root;
static WCHAR g_prefix[1024];     /* "\??\X:\...\Stock Game" */
static size_t g_prefix_len;
static SRWLOCK g_lock = SRWLOCK_INIT;
static volatile LONG g_no_fail;  /* set by a rename, or by the environment */
static uint64_t g_entries, g_dirs;

static void plog(const char *fmt, ...);
static _Thread_local int t_bypass;
static Dir *g_pending;              /* folders waiting to be indexed, under g_lock */  /* the index scanning: let its own calls through */
static volatile LONG64 c_calls, c_exact, c_fixed, c_missing, c_pass, c_added;

typedef WCHAR (NTAPI *RtlUpcaseUnicodeChar_t)(WCHAR);
static RtlUpcaseUnicodeChar_t pRtlUpcaseUnicodeChar;

static inline WCHAR up(WCHAR c) {
    if (c < 128) return (c >= 'a' && c <= 'z') ? (WCHAR)(c - 32) : c;
    return pRtlUpcaseUnicodeChar ? pRtlUpcaseUnicodeChar(c) : c;
}

static uint32_t hash_upper(const WCHAR *s, size_t n, WCHAR *out) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < n; i++) {
        WCHAR c = up(s[i]);
        if (out) out[i] = c;
        h = (h ^ c) * 16777619u;
    }
    return h;
}

static void *alloc(size_t n) { return HeapAlloc(g_heap, HEAP_ZERO_MEMORY, n); }

static Dir *dir_new(uint32_t nb) {
    Dir *d = alloc(sizeof *d);
    d->nbuckets = nb;
    d->buckets = alloc(sizeof(Entry *) * nb);
    return d;
}

static void dir_grow(Dir *d) {
    uint32_t nb = d->nbuckets * 4;
    Entry **b = alloc(sizeof(Entry *) * nb);
    for (uint32_t i = 0; i < d->nbuckets; i++) {
        for (Entry *e = d->buckets[i], *nx; e; e = nx) {
            nx = e->next;
            e->next = b[e->hash % nb];
            b[e->hash % nb] = e;
        }
    }
    HeapFree(g_heap, 0, d->buckets);
    d->buckets = b;
    d->nbuckets = nb;
}

static Entry *dir_find(Dir *d, const WCHAR *name, size_t n, uint32_t h, const WCHAR *upper) {
    for (Entry *e = d->buckets[h % d->nbuckets]; e; e = e->next)
        if (e->hash == h && e->len == n && !wmemcmp(e->upper, upper, n)) return e;
    (void)name;
    return NULL;
}

static Entry *dir_add_ex(Dir *d, const WCHAR *name, size_t n, BOOL is_dir, BOOL *created) {
    WCHAR *mem = alloc(sizeof(WCHAR) * (2 * n + 2));
    uint32_t h = hash_upper(name, n, mem + n + 1);
    Entry *e = dir_find(d, name, n, h, mem + n + 1);
    if (created) *created = e == NULL;
    if (e) {
        HeapFree(g_heap, 0, mem);
        if (is_dir && !e->dir) e->dir = dir_new(8);   /* a file became a folder */
        return e;
    }
    e = alloc(sizeof *e);
    wmemcpy(mem, name, n);
    e->exact = mem;
    e->upper = mem + n + 1;
    e->len = (uint16_t)n;
    e->hash = h;
    e->dir = is_dir ? dir_new(8) : NULL;
    if (++d->count > d->nbuckets * 2) dir_grow(d);
    e->next = d->buckets[h % d->nbuckets];
    d->buckets[h % d->nbuckets] = e;
    return e;
}

static Entry *dir_add(Dir *d, const WCHAR *name, size_t n, BOOL is_dir) {
    return dir_add_ex(d, name, n, is_dir, NULL);
}

static void scan(Dir *d, WCHAR *path, size_t len) {
    if (len + 3 >= 32000) return;
    wcscpy(path + len, L"\\*");
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileExW(path, FindExInfoBasic, &fd, FindExSearchNameMatch, NULL,
                                FIND_FIRST_EX_LARGE_FETCH);
    path[len] = 0;
    if (h == INVALID_HANDLE_VALUE) return;
    do {
        const WCHAR *n = fd.cFileName;
        if (n[0] == '.' && (!n[1] || (n[1] == '.' && !n[2]))) continue;
        size_t nl = wcslen(n);
        BOOL is_dir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
        Entry *e = dir_add(d, n, nl, is_dir);
        g_entries++;
        if (is_dir && len + 1 + nl < 32000) {
            g_dirs++;
            path[len] = '\\';
            wmemcpy(path + len + 1, n, nl);
            path[len + 1 + nl] = 0;
            scan(e->dir, path, len + 1 + nl);
            path[len] = 0;
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
}

/* ------------------------------------------------- Mosaic's file list */

/* Mosaic writes <game>\Data\SKSE\Plugins\MosaicPathIndex.idx at deploy, UTF-8:
 *   MOSAIC-PATHINDEX 1
 *   D <last-write FILETIME> <folder, relative, '\'-separated; "." = game folder>
 *   F <file, relative>
 * Loading it is much faster than listing ~250K entries through Wine. Anything
 * may have changed since the deploy (tools, the game, the GUI's root files),
 * so every folder's last-write time is compared and changed folders are
 * listed again: a folder whose entries changed always has a newer time. */

static uint64_t g_list_dirs, g_rescanned, g_rescan_added;

static Dir *walk_create(const WCHAR *rel, size_t n) {
    Dir *d = g_root;
    size_t i = 0;
    while (i < n && d) {
        size_t s = i;
        while (i < n && rel[i] != '\\') i++;
        if (i > s && !(i - s == 1 && rel[s] == '.')) {
            Entry *e = dir_add(d, rel + s, i - s, TRUE);
            d = e->dir;
        }
        i++;
    }
    return d;
}

static BOOL load_list(const WCHAR *file) {
    HANDLE h = CreateFileW(file, GENERIC_READ, FILE_SHARE_READ, NULL, OPEN_EXISTING, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return FALSE;
    LARGE_INTEGER sz;
    if (!GetFileSizeEx(h, &sz) || sz.QuadPart <= 0 || sz.QuadPart > (1LL << 31)) {
        CloseHandle(h);
        return FALSE;
    }
    char *buf = HeapAlloc(GetProcessHeap(), 0, (SIZE_T)sz.QuadPart + 1);
    DWORD got = 0;
    BOOL ok = buf && ReadFile(h, buf, (DWORD)sz.QuadPart, &got, NULL) && got == sz.QuadPart;
    CloseHandle(h);
    if (!ok) { if (buf) HeapFree(GetProcessHeap(), 0, buf); return FALSE; }
    buf[got] = 0;
    static const char magic[] = "MOSAIC-PATHINDEX 1\n";
    if (strncmp(buf, magic, sizeof magic - 1)) { HeapFree(GetProcessHeap(), 0, buf); return FALSE; }

    static WCHAR w[32768];
    char *line = buf + sizeof magic - 1;
    while (*line) {
        char *end = strchr(line, '\n');
        if (!end) end = line + strlen(line);
        char kind = line[0];
        char *p = line + 2;
        uint64_t ft = 0;
        if (kind == 'D') {
            while (*p >= '0' && *p <= '9') ft = ft * 10 + (uint64_t)(*p++ - '0');
            if (*p == ' ') p++;
        }
        int n = (end > p && (kind == 'D' || kind == 'F'))
                    ? MultiByteToWideChar(CP_UTF8, 0, p, (int)(end - p), w, 32767) : 0;
        if (n > 0 || (kind == 'D' && end == p)) {
            if (kind == 'D') {
                Dir *d = walk_create(w, (size_t)n);
                if (d) { d->ft = ft; g_list_dirs++; }
            } else {
                size_t slash = (size_t)n;
                while (slash > 0 && w[slash - 1] != '\\') slash--;
                Dir *d = slash ? walk_create(w, slash - 1) : g_root;
                if (d) { dir_add(d, w + slash, (size_t)n - slash, FALSE); g_entries++; }
            }
        }
        line = *end ? end + 1 : end;
    }
    HeapFree(GetProcessHeap(), 0, buf);
    return TRUE;
}

/* List `path` again and add what the index doesn't have; a folder that is new
 * is indexed whole. Folders already known are verified on their own. */
static void rescan_dir(Dir *d, WCHAR *path, size_t len) {
    if (len + 3 >= 32000) return;
    wcscpy(path + len, L"\\*");
    WIN32_FIND_DATAW fd;
    HANDLE h = FindFirstFileExW(path, FindExInfoBasic, &fd, FindExSearchNameMatch, NULL,
                                FIND_FIRST_EX_LARGE_FETCH);
    path[len] = 0;
    if (h == INVALID_HANDLE_VALUE) return;
    g_rescanned++;
    do {
        const WCHAR *n = fd.cFileName;
        if (n[0] == '.' && (!n[1] || (n[1] == '.' && !n[2]))) continue;
        size_t nl = wcslen(n);
        BOOL is_dir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
        BOOL created = FALSE;
        Entry *e = dir_add_ex(d, n, nl, is_dir, &created);
        if (!created) continue;
        g_rescan_added++;
        if (is_dir && e->dir && len + 1 + nl < 32000) {
            path[len] = '\\';
            wmemcpy(path + len + 1, n, nl);
            path[len + 1 + nl] = 0;
            scan(e->dir, path, len + 1 + nl);
            e->dir->ft = 0;                          /* scanned live: nothing to check */
            path[len] = 0;
        }
    } while (FindNextFileW(h, &fd));
    FindClose(h);
}

static void verify(Dir *d, WCHAR *path, size_t len) {
    if (d->ft) {
        WIN32_FILE_ATTRIBUTE_DATA ad;
        if (GetFileAttributesExW(path, GetFileExInfoStandard, &ad)) {
            uint64_t now = ((uint64_t)ad.ftLastWriteTime.dwHighDateTime << 32) |
                           ad.ftLastWriteTime.dwLowDateTime;
            if (now != d->ft) rescan_dir(d, path, len);
        }
    }
    for (uint32_t b = 0; b < d->nbuckets; b++) {
        for (Entry *e = d->buckets[b]; e; e = e->next) {
            if (!e->dir || !e->dir->ft || len + 1 + e->len >= 32000) continue;
            path[len] = '\\';
            wmemcpy(path + len + 1, e->exact, e->len);
            path[len + 1 + e->len] = 0;
            verify(e->dir, path, len + 1 + e->len);
            path[len] = 0;
        }
    }
}

/* ----------------------------------------------------------------- lookup */

enum { R_PASS, R_EXACT, R_FIXED, R_LEAF_MISSING, R_PATH_MISSING };

/* Resolve the part of `path` after the game folder. On R_FIXED, `out` holds the
 * whole path with real casing. `*parent` is set to the directory that would
 * hold a missing leaf (R_LEAF_MISSING), for adding a created file. */
static int resolve(const WCHAR *path, size_t len, WCHAR *out, size_t outcap,
                   Dir **parent, const WCHAR **leaf, size_t *leaf_len) {
    if (len < g_prefix_len || g_prefix_len == 0) return R_PASS;
    for (size_t i = 0; i < g_prefix_len; i++)
        if (up(path[i]) != up(g_prefix[i])) return R_PASS;
    if (len > g_prefix_len && path[g_prefix_len] != '\\' && path[g_prefix_len] != '/')
        return R_PASS;
    for (size_t i = g_prefix_len; i < len; i++)
        if (path[i] == '*' || path[i] == '?' || path[i] == '<' || path[i] == '>' ||
            path[i] == '"' || path[i] == ':')
            return R_PASS;

    size_t o = g_prefix_len;
    if (o + 1 >= outcap) return R_PASS;
    wmemcpy(out, g_prefix, o);
    BOOL changed = FALSE;
    Dir *d = g_root;
    size_t i = g_prefix_len;
    WCHAR ubuf[512];
    for (;;) {
        while (i < len && (path[i] == '\\' || path[i] == '/')) i++;
        if (i >= len) break;
        size_t s = i;
        while (i < len && path[i] != '\\' && path[i] != '/') i++;
        size_t n = i - s;
        const WCHAR *comp = path + s;
        if ((n == 1 && comp[0] == '.') || (n == 2 && comp[0] == '.' && comp[1] == '.'))
            return R_PASS;
        if (n >= 512) return R_PASS;
        size_t rest = i;
        while (rest < len && (path[rest] == '\\' || path[rest] == '/')) rest++;
        BOOL last = rest >= len;
        if (!d) return R_PATH_MISSING;        /* a file used as a folder */
        if (d->pending) return R_PASS;        /* not indexed yet: Wine decides */
        uint32_t h = hash_upper(comp, n, ubuf);
        Entry *e = dir_find(d, comp, n, h, ubuf);
        if (!e) {
            if (last) {
                *parent = d; *leaf = comp; *leaf_len = n;
                return R_LEAF_MISSING;
            }
            return R_PATH_MISSING;
        }
        if (o + 1 + n + 1 >= outcap) return R_PASS;
        out[o++] = '\\';
        wmemcpy(out + o, e->exact, n);
        o += n;
        if (wmemcmp(e->exact, comp, n)) changed = TRUE;
        d = e->dir;
    }
    out[o] = 0;
    /* keep a trailing separator the caller asked for */
    if (len > 0 && (path[len - 1] == '\\' || path[len - 1] == '/') && o + 1 < outcap) {
        out[o++] = '\\';
        out[o] = 0;
    }
    if (changed) { *leaf_len = o; return R_FIXED; }
    return R_EXACT;
}

/* TRUE when `path` is the game folder or inside it. */
static BOOL len_prefixed(const WCHAR *path, size_t len) {
    if (len < g_prefix_len || !g_prefix_len) return FALSE;
    for (size_t i = 0; i < g_prefix_len; i++)
        if (up(path[i]) != up(g_prefix[i])) return FALSE;
    return len == g_prefix_len || path[g_prefix_len] == '\\' || path[g_prefix_len] == '/';
}

/* ------------------------------------------------------------------ hooks */

typedef NTSTATUS(NTAPI *NtCreateFile_t)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES,
                                        PIO_STATUS_BLOCK, PLARGE_INTEGER, ULONG, ULONG,
                                        ULONG, ULONG, PVOID, ULONG);
typedef NTSTATUS(NTAPI *NtOpenFile_t)(PHANDLE, ACCESS_MASK, POBJECT_ATTRIBUTES,
                                      PIO_STATUS_BLOCK, ULONG, ULONG);
typedef NTSTATUS(NTAPI *NtQueryAttributesFile_t)(POBJECT_ATTRIBUTES, PVOID);
typedef NTSTATUS(NTAPI *NtSetInformationFile_t)(HANDLE, PIO_STATUS_BLOCK, PVOID, ULONG,
                                                ULONG);

static NtCreateFile_t oNtCreateFile;
static NtOpenFile_t oNtOpenFile;
static NtQueryAttributesFile_t oNtQueryAttributesFile;
static NtQueryAttributesFile_t oNtQueryFullAttributesFile;
static NtSetInformationFile_t oNtSetInformationFile;

#define PATHBUF 4096

typedef struct {
    int r;
    OBJECT_ATTRIBUTES oa;
    UNICODE_STRING us;
    Dir *parent;
    const WCHAR *leaf;
    size_t leaf_len;
    WCHAR buf[PATHBUF];
} Lookup;

/* Fills lk; returns the OBJECT_ATTRIBUTES to pass on (the caller's, or a copy
 * pointing at the case-fixed path). */
static POBJECT_ATTRIBUTES lookup(POBJECT_ATTRIBUTES oa, Lookup *lk) {
    InterlockedIncrement64(&c_calls);
    lk->r = R_PASS;
    if (t_bypass || !oa || oa->RootDirectory || !oa->ObjectName || !oa->ObjectName->Buffer) {
        InterlockedIncrement64(&c_pass);
        return oa;
    }
    AcquireSRWLockShared(&g_lock);
    lk->r = resolve(oa->ObjectName->Buffer, oa->ObjectName->Length / sizeof(WCHAR), lk->buf,
                    PATHBUF, &lk->parent, &lk->leaf, &lk->leaf_len);
    ReleaseSRWLockShared(&g_lock);
    switch (lk->r) {
    case R_EXACT: InterlockedIncrement64(&c_exact); return oa;
    case R_FIXED:
        InterlockedIncrement64(&c_fixed);
        lk->oa = *oa;
        lk->us.Buffer = lk->buf;
        lk->us.Length = (USHORT)(lk->leaf_len * sizeof(WCHAR));
        lk->us.MaximumLength = lk->us.Length + sizeof(WCHAR);
        lk->oa.ObjectName = &lk->us;
        return &lk->oa;
    case R_LEAF_MISSING:
    case R_PATH_MISSING: InterlockedIncrement64(&c_missing); return oa;
    default: InterlockedIncrement64(&c_pass); return oa;
    }
}

static BOOL answer_missing(const Lookup *lk, NTSTATUS *st) {
    if (g_no_fail) return FALSE;
    if (lk->r == R_LEAF_MISSING) { *st = STATUS_OBJECT_NAME_NOT_FOUND; return TRUE; }
    if (lk->r == R_PATH_MISSING) { *st = STATUS_OBJECT_PATH_NOT_FOUND; return TRUE; }
    return FALSE;
}

/* `nt_path` set: a folder that arrived with contents (renamed into place),
 * which are indexed too. */
static void remember_created(const Lookup *lk, BOOL is_dir, const WCHAR *nt_path,
                             size_t nt_len) {
    if (lk->r != R_LEAF_MISSING || !lk->parent) return;
    AcquireSRWLockExclusive(&g_lock);
    Entry *e = dir_add(lk->parent, lk->leaf, lk->leaf_len, is_dir);
    if (e->dir && nt_path && nt_len > 4 && nt_len < 32000 && !e->dir->pending) {
        /* Its contents can't be listed yet -- the rename still holds the
         * folder open -- so pending_thread indexes them a moment later. */
        WCHAR *pth = alloc(sizeof(WCHAR) * (nt_len - 4 + 1));
        wmemcpy(pth, nt_path + 4, nt_len - 4);       /* drop the "\??\" prefix */
        e->dir->pending = pth;
        e->dir->next_pending = g_pending;
        g_pending = e->dir;
    }
    ReleaseSRWLockExclusive(&g_lock);
    InterlockedIncrement64(&c_added);
}

static NTSTATUS NTAPI hNtCreateFile(PHANDLE h, ACCESS_MASK a, POBJECT_ATTRIBUTES oa,
                                    PIO_STATUS_BLOCK io, PLARGE_INTEGER sz, ULONG attr,
                                    ULONG share, ULONG disp, ULONG opts, PVOID ea, ULONG eal) {
    Lookup lk;
    POBJECT_ATTRIBUTES use = lookup(oa, &lk);
    BOOL may_create = disp != PI_FILE_OPEN && disp != PI_FILE_OVERWRITE;
    NTSTATUS st;
    if (!may_create && answer_missing(&lk, &st)) {
        if (io) { io->Status = st; io->Information = 0; }
        return st;
    }
    st = oNtCreateFile(h, a, use, io, sz, attr, share, disp, opts, ea, eal);
    if (may_create && st >= 0)
        remember_created(&lk, (opts & PI_FILE_DIRECTORY_FILE) != 0, NULL, 0);
    return st;
}

static NTSTATUS NTAPI hNtOpenFile(PHANDLE h, ACCESS_MASK a, POBJECT_ATTRIBUTES oa,
                                  PIO_STATUS_BLOCK io, ULONG share, ULONG opts) {
    Lookup lk;
    POBJECT_ATTRIBUTES use = lookup(oa, &lk);
    NTSTATUS st;
    if (answer_missing(&lk, &st)) {
        if (io) { io->Status = st; io->Information = 0; }
        return st;
    }
    return oNtOpenFile(h, a, use, io, share, opts);
}

static NTSTATUS NTAPI hNtQueryAttributesFile(POBJECT_ATTRIBUTES oa, PVOID info) {
    Lookup lk;
    POBJECT_ATTRIBUTES use = lookup(oa, &lk);
    NTSTATUS st;
    if (answer_missing(&lk, &st)) return st;
    return oNtQueryAttributesFile(use, info);
}

static NTSTATUS NTAPI hNtQueryFullAttributesFile(POBJECT_ATTRIBUTES oa, PVOID info) {
    Lookup lk;
    POBJECT_ATTRIBUTES use = lookup(oa, &lk);
    NTSTATUS st;
    if (answer_missing(&lk, &st)) return st;
    return oNtQueryFullAttributesFile(use, info);
}

/* FILE_RENAME_INFORMATION / FILE_LINK_INFORMATION (same layout, x64). */
typedef struct {
    ULONG flags;            /* ReplaceIfExists (BOOLEAN) or Flags */
    HANDLE root;
    ULONG name_len;         /* bytes */
    WCHAR name[1];
} RenameInfo;

static NTSTATUS NTAPI hNtSetInformationFile(HANDLE h, PIO_STATUS_BLOCK io, PVOID info,
                                            ULONG len, ULONG cls) {
    if (cls != PI_FileRenameInformation && cls != PI_FileRenameInformationEx &&
        cls != PI_FileLinkInformation && cls != PI_FileLinkInformationEx)
        return oNtSetInformationFile(h, io, info, len, cls);

    /* A file gets a new name. Outside the game folder (saves, My Games) the
     * index doesn't care; inside it, the new name is added once the rename
     * succeeds. Anything that can't be followed switches the "not found"
     * answers off for the rest of the session, so a miss is never wrong. */
    const RenameInfo *ri = (const RenameInfo *)info;
    Lookup lk;
    lk.r = R_PASS;
    BOOL follow = FALSE;
    if (info && len >= offsetof(RenameInfo, name) && !ri->root &&
        ri->name_len <= len - offsetof(RenameInfo, name)) {
        AcquireSRWLockShared(&g_lock);
        lk.r = resolve(ri->name, ri->name_len / sizeof(WCHAR), lk.buf, PATHBUF, &lk.parent,
                       &lk.leaf, &lk.leaf_len);
        ReleaseSRWLockShared(&g_lock);
        follow = lk.r != R_PATH_MISSING &&
                 !(lk.r == R_PASS && len_prefixed(ri->name, ri->name_len / sizeof(WCHAR)));
    }
    if (!follow) InterlockedExchange(&g_no_fail, 1);
    NTSTATUS st = oNtSetInformationFile(h, io, info, len, cls);
    if (st >= 0 && lk.r == R_LEAF_MISSING) {
        /* folder or file? Ask by the new name: the handle a rename uses is
         * often opened with DELETE access only, which can't read attributes. */
        struct { LARGE_INTEGER t[4]; ULONG attrs; } bi;
        UNICODE_STRING us = {(USHORT)ri->name_len, (USHORT)ri->name_len, (PWSTR)ri->name};
        OBJECT_ATTRIBUTES oa;
        InitializeObjectAttributes(&oa, &us, OBJ_CASE_INSENSITIVE, NULL, NULL);
        BOOL is_dir = oNtQueryAttributesFile(&oa, &bi) >= 0 &&
                      (bi.attrs & FILE_ATTRIBUTE_DIRECTORY);
        remember_created(&lk, is_dir, ri->name, ri->name_len / sizeof(WCHAR));
    }
    return st;
}

/* Point `mod`'s import of ntdll!name at `fn`. Returns TRUE if found. */
static BOOL patch_iat(HMODULE mod, const char *name, void *fn) {
    BYTE *base = (BYTE *)mod;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    IMAGE_DATA_DIRECTORY dd = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dd.VirtualAddress) return FALSE;
    BOOL found = FALSE;
    for (IMAGE_IMPORT_DESCRIPTOR *imp = (IMAGE_IMPORT_DESCRIPTOR *)(base + dd.VirtualAddress);
         imp->Name; imp++) {
        if (_stricmp((const char *)(base + imp->Name), "ntdll.dll")) continue;
        IMAGE_THUNK_DATA *names = (IMAGE_THUNK_DATA *)(base + (imp->OriginalFirstThunk
                                                               ? imp->OriginalFirstThunk
                                                               : imp->FirstThunk));
        IMAGE_THUNK_DATA *iat = (IMAGE_THUNK_DATA *)(base + imp->FirstThunk);
        for (; names->u1.AddressOfData; names++, iat++) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            IMAGE_IMPORT_BY_NAME *ibn = (IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData);
            if (strcmp((const char *)ibn->Name, name)) continue;
            DWORD old;
            if (VirtualProtect(&iat->u1.Function, sizeof(void *), PAGE_READWRITE, &old)) {
                iat->u1.Function = (ULONG_PTR)fn;
                VirtualProtect(&iat->u1.Function, sizeof(void *), old, &old);
                found = TRUE;
            }
        }
    }
    return found;
}

/* ------------------------------------------------------------------- log */

static HANDLE g_log = INVALID_HANDLE_VALUE;

static void plog(const char *fmt, ...) {
    if (g_log == INVALID_HANDLE_VALUE) return;
    char buf[1024];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (n > 0) {
        DWORD w;
        WriteFile(g_log, buf, (DWORD)(n < (int)sizeof buf ? n : (int)sizeof buf - 1), &w, NULL);
    }
}

/* Index the contents of folders that were renamed into the game folder. */
static DWORD WINAPI pending_thread(void *unused) {
    (void)unused;
    static WCHAR path[32768];
    t_bypass = 1;
    for (;;) {
        Sleep(250);
        AcquireSRWLockExclusive(&g_lock);
        Dir *d = g_pending, *keep = NULL;
        g_pending = NULL;
        while (d) {
            Dir *nx = d->next_pending;
            size_t n = wcslen(d->pending);
            wcscpy(path, d->pending);
            wcscpy(path + n, L"\\*");
            WIN32_FIND_DATAW fd;
            HANDLE h = FindFirstFileExW(path, FindExInfoBasic, &fd, FindExSearchNameMatch,
                                        NULL, 0);
            if (h == INVALID_HANDLE_VALUE && GetLastError() == ERROR_SHARING_VIOLATION) {
                d->next_pending = keep;              /* still held open: next round */
                keep = d;
            } else {
                if (h != INVALID_HANDLE_VALUE) FindClose(h);
                path[n] = 0;
                scan(d, path, n);
                HeapFree(g_heap, 0, d->pending);
                d->pending = NULL;
            }
            d = nx;
        }
        g_pending = keep;
        ReleaseSRWLockExclusive(&g_lock);
    }
    return 0;
}

static DWORD WINAPI stats_thread(void *unused) {
    (void)unused;
    DWORD start = GetTickCount();
    for (;;) {
        Sleep(15000);
        plog("[%6lus] calls %lld: exact %lld, case fixed %lld, missing %lld%s, passed %lld, "
             "added %lld\n",
             (unsigned long)((GetTickCount() - start) / 1000), c_calls, c_exact, c_fixed,
             c_missing, g_no_fail ? " (passed on)" : " (answered)", c_pass, c_added);
    }
    return 0;
}

/* ------------------------------------------------------------------ setup */

static BOOL env_is(const char *name, const char *val) {
    char b[16];
    DWORD n = GetEnvironmentVariableA(name, b, sizeof b);
    return n > 0 && n < sizeof b && !strcmp(b, val);
}

static void start(void) {
    WCHAR root[32768];
    DWORD n = GetModuleFileNameW(NULL, root, 32000);
    if (!n || n >= 32000) return;
    WCHAR *slash = wcsrchr(root, '\\');
    if (!slash) return;
    *slash = 0;
    size_t rl = wcslen(root);
    while (rl > 3 && root[rl - 1] == '\\') root[--rl] = 0;     /* "Stock Game\\" */

    /* The log and the file list sit next to this DLL: Data\<Extender>\Plugins. */
    static WCHAR self[32768];
    HMODULE me = NULL;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCWSTR)(void *)start, &me);
    DWORD sn = GetModuleFileNameW(me, self, 32000);
    WCHAR *sslash = sn && sn < 32000 ? wcsrchr(self, '\\') : NULL;
    if (!sslash) return;
    *sslash = 0;
    WCHAR logpath[32768];
    _snwprintf(logpath, 32768, L"%ls\\MosaicPathIndex.log", self);
    g_log = CreateFileW(logpath, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    plog("MosaicPathIndex 1 (%d-bit) -- game folder %ls\n", (int)(sizeof(void *) * 8), root);
    if (env_is("MOSAIC_PATHINDEX", "0")) { plog("disabled by MOSAIC_PATHINDEX=0\n"); return; }
    if (env_is("MOSAIC_PATHINDEX_NOFAIL", "1")) {
        g_no_fail = 1;
        plog("MOSAIC_PATHINDEX_NOFAIL=1: case fixing only\n");
    }

    HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
    pRtlUpcaseUnicodeChar = (RtlUpcaseUnicodeChar_t)(void *)GetProcAddress(ntdll, "RtlUpcaseUnicodeChar");
    oNtCreateFile = (NtCreateFile_t)(void *)GetProcAddress(ntdll, "NtCreateFile");
    oNtOpenFile = (NtOpenFile_t)(void *)GetProcAddress(ntdll, "NtOpenFile");
    oNtQueryAttributesFile = (NtQueryAttributesFile_t)(void *)GetProcAddress(ntdll, "NtQueryAttributesFile");
    oNtQueryFullAttributesFile =
        (NtQueryAttributesFile_t)(void *)GetProcAddress(ntdll, "NtQueryFullAttributesFile");
    oNtSetInformationFile = (NtSetInformationFile_t)(void *)GetProcAddress(ntdll, "NtSetInformationFile");
    if (!oNtCreateFile || !oNtOpenFile || !oNtQueryAttributesFile ||
        !oNtQueryFullAttributesFile || !oNtSetInformationFile) {
        plog("ntdll file functions not found; not hooking\n");
        return;
    }

    g_heap = HeapCreate(HEAP_NO_SERIALIZE, 1 << 24, 0);
    if (!g_heap) return;
    LARGE_INTEGER f, t0, t1;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t0);
    g_root = dir_new(64);
    static WCHAR path[32768];
    WCHAR listpath[32768];
    _snwprintf(listpath, 32768, L"%ls\\MosaicPathIndex.idx", self);
    if (load_list(listpath)) {
        LARGE_INTEGER tl;
        QueryPerformanceCounter(&tl);
        wcscpy(path, root);
        verify(g_root, path, rl);
        QueryPerformanceCounter(&t1);
        plog("loaded Mosaic's file list: %llu entries in %llu folders in %.2f s; checked them "
             "in %.2f s, listed %llu changed folder(s) again, %llu new entries\n",
             (unsigned long long)g_entries, (unsigned long long)g_list_dirs,
             (double)(tl.QuadPart - t0.QuadPart) / f.QuadPart,
             (double)(t1.QuadPart - tl.QuadPart) / f.QuadPart,
             (unsigned long long)g_rescanned, (unsigned long long)g_rescan_added);
    } else {
        wcscpy(path, root);
        scan(g_root, path, rl);
        QueryPerformanceCounter(&t1);
        plog("no file list from Mosaic; indexed %llu entries in %llu folders in %.2f s\n",
             (unsigned long long)g_entries, (unsigned long long)g_dirs,
             (double)(t1.QuadPart - t0.QuadPart) / f.QuadPart);
    }
    /* HEAP_NO_SERIALIZE was only for the single-threaded build; later adds
     * happen under g_lock, so the heap is never used concurrently. */

    _snwprintf(g_prefix, 1024, L"\\??\\%ls", root);
    g_prefix_len = wcslen(g_prefix);

    static const struct { const char *name; void *fn; } hooks[] = {
        {"NtCreateFile", (void *)hNtCreateFile},
        {"NtOpenFile", (void *)hNtOpenFile},
        {"NtQueryAttributesFile", (void *)hNtQueryAttributesFile},
        {"NtQueryFullAttributesFile", (void *)hNtQueryFullAttributesFile},
        {"NtSetInformationFile", (void *)hNtSetInformationFile},
    };
    static const WCHAR *mods[] = {L"kernelbase.dll", L"kernel32.dll"};
    for (size_t m = 0; m < sizeof mods / sizeof *mods; m++) {
        HMODULE mod = GetModuleHandleW(mods[m]);
        if (!mod) continue;
        for (size_t k = 0; k < sizeof hooks / sizeof *hooks; k++)
            plog("hook %ls!%s: %s\n", mods[m], hooks[k].name,
                 patch_iat(mod, hooks[k].name, hooks[k].fn) ? "on" : "not imported");
    }
    CloseHandle(CreateThread(NULL, 0, stats_thread, NULL, 0, NULL));
    CloseHandle(CreateThread(NULL, 0, pending_thread, NULL, 0, NULL));
}

/* --------------------------------------------------- script extenders
 * Each extender looks up only its own names, so one DLL can carry them all.
 *
 * Newer ones read a version block before loading anything: SKSE64 for Skyrim
 * 1.6+ its own (above), F4SE for Fallout 4 NG and SFSE the same layout as
 * each other (below). "No hardcoded addresses" plus "no game structures"
 * makes a plugin load on every game version -- true here: it only patches
 * Windows' own DLLs.
 *
 * The rest -- Skyrim SE 1.5.97, Skyrim VR, Fallout 4 before NG and VR, and
 * every 32-bit extender (SKSE, NVSE, FOSE, OBSE) -- call <X>Plugin_Query
 * with a PluginInfo to fill in, then <X>Plugin_Load. */

typedef struct {
    uint32_t dataVersion;
    uint32_t pluginVersion;
    char name[256];
    char author[256];
    uint32_t addressIndependence;   /* 1 = signatures only: no hardcoded addresses */
    uint32_t structureIndependence; /* 1 = uses no game structures */
    uint32_t compatibleVersions[16];
    uint32_t seVersionRequired;
    uint32_t reservedNonBreaking;
    uint32_t reservedBreaking;
} XSEPluginVersionData;

__declspec(dllexport) XSEPluginVersionData F4SEPlugin_Version = {
    1, 1, "MosaicPathIndex", "Mosaic Mod Manager", 1, 1, {0}, 0, 0, 0,
};
__declspec(dllexport) XSEPluginVersionData SFSEPlugin_Version = {
    1, 1, "MosaicPathIndex", "Mosaic Mod Manager", 1, 1, {0}, 0, 0, 0,
};

typedef struct { uint32_t infoVersion; const char *name; uint32_t version; } XSEPluginInfo;

static _Bool query(XSEPluginInfo *info) {
    if (info) { info->infoVersion = 1; info->name = "MosaicPathIndex"; info->version = 1; }
    return 1;
}

static _Bool load(void) {
    static LONG once;
    if (InterlockedExchange(&once, 1)) return 1;
    start();
    return 1;
}

#define XSE_PLUGIN(X)                                                              \
    __declspec(dllexport) _Bool X##Plugin_Query(const void *xse, XSEPluginInfo *info) { \
        (void)xse;                                                                 \
        return query(info);                                                        \
    }                                                                              \
    __declspec(dllexport) _Bool X##Plugin_Load(const void *xse) {                  \
        (void)xse;                                                                 \
        return load();                                                             \
    }

XSE_PLUGIN(SKSE)
XSE_PLUGIN(F4SE)
XSE_PLUGIN(SFSE)
XSE_PLUGIN(NVSE)
XSE_PLUGIN(FOSE)
XSE_PLUGIN(OBSE)

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(inst);
    return TRUE;
}
