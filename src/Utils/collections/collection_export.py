"""collection_export.py
Build a real Nexus/Vortex-compatible collection archive (.7z) from a
profile, for local Vortex import or publishing via NexusAPI's collection
upload methods (see Nexus/nexus_api.py: get_collection_upload_url /
upload_collection_archive / create_collection / create_or_update_revision /
publish_revision).

The manifest format mirrors real Vortex collections: top-level info / mods /
modRules / plugins / pluginRules. Mosaic's own collection-install pipeline
(collection_install.py / collection_reset.py) already fully consumes this
shape, so an export round-trips back into Mosaic too.

Rows come from Utils.profile.profile_export.load_rows plus per-row export
flags (source / direct_url / optional). All functions are toolkit-free; the
Qt view drives them from a worker thread.

FOMOD/BAIN installer choices are embedded via
Utils.profile.profile_export.resolve_installer_choices (the same sidecar
resolution the .mosaic manifest uses) when a sidecar exists; otherwise the
mod is exported without choices and a warning is raised, since it will be
asked interactively on reimport.

Deliberately deferred for this first version (not ported): binary-patch
file-edit diffs (bsdiff4), INI-tweak bundling, and conflict-derived modRules
(Mosaic has no equivalent of the reference implementation's
FileGraphService). modRules here are a minimal adjacent-mod chain instead —
see build_mod_rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from Nexus.nexus_meta import normalise_game_domain
from Utils.config_paths import get_download_cache_dir
from Utils.profile.profile_export import resolve_installer_choices

PHASE_META = "meta"
PHASE_HASH = "hash"
PHASE_BUNDLE = "bundle"
PHASE_PACK = "pack"

UPDATE_POLICIES = ("exact", "prefer", "latest")

COLLECTION_NAME_MIN = 3
COLLECTION_NAME_MAX = 36

# The archive goes up as ONE presigned PUT (NexusAPI.upload_collection_archive),
# and S3-compatible storage — Nexus hands out Backblaze B2 URLs — caps a single
# PutObject at 5 GiB. Past that you need multipart, which we don't implement,
# so the upload is rejected by the storage layer, not by Nexus.
UPLOAD_SIZE_LIMIT = 5 * 1024 ** 3
UPLOAD_SIZE_WARN = 5_000_000_000

FAST_PACK_THRESHOLD = 64 * 1024 * 1024
INCOMPRESSIBLE_RATIO = 0.95
_SAMPLE_BYTES = 4 * 1024 * 1024
_SAMPLE_FILES = 16


def format_bytes(n: int) -> str:
    """Bytes as a short human string ("1.4 GB")."""
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(value) < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


def _bundle_totals(bundle_jobs) -> "list[tuple[str, int]]":
    totals: dict[str, int] = {}
    for src, arcname in bundle_jobs or ():
        parts = str(arcname).replace("\\", "/").split("/")
        key = parts[1] if (len(parts) > 2 and parts[0] == "bundled") else parts[0]
        try:
            totals[key] = totals.get(key, 0) + Path(src).stat().st_size
        except OSError:
            continue
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def check_upload_size(archive_path, bundle_jobs=None) -> "tuple[bool, str]":
    """Whether a packed archive can be uploaded — (ok, message). ok False
    means the upload cannot succeed; ok True with a message is advisory."""
    try:
        size = Path(archive_path).stat().st_size
    except OSError:
        return True, ""
    if size <= UPLOAD_SIZE_WARN:
        return True, ""
    biggest = _bundle_totals(bundle_jobs)[:3]
    detail = ""
    if biggest:
        detail = " Largest bundled: " + ", ".join(
            f"{name} ({format_bytes(nbytes)})" for name, nbytes in biggest) + "."
    if size > UPLOAD_SIZE_LIMIT:
        return False, (
            f"The collection archive is {format_bytes(size)}, over the "
            f"{format_bytes(UPLOAD_SIZE_LIMIT)} limit for a single upload — "
            f"un-bundle something, or host the largest output as its own mod "
            f"page and require it instead.{detail}")
    return True, (
        f"The collection archive is {format_bytes(size)}, close to the "
        f"{format_bytes(UPLOAD_SIZE_LIMIT)} single-upload limit — Nexus may "
        f"refuse it, and a refusal costs the whole transfer.{detail}")


def read_profile_manifest(profile_dir) -> dict:
    """The collection manifest saved to <profile>/collection.json, when the
    profile was installed from a real collection. Used to seed a re-upload
    from the original authoring settings, and to reuse original modRules."""
    if not profile_dir:
        return {}
    path = Path(profile_dir) / "collection.json"
    if not path.is_file():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def validate_collection_name(name: str) -> str:
    """"" when *name* is acceptable, else a human-readable reason."""
    text = (name or "").strip()
    if len(text) < COLLECTION_NAME_MIN:
        return f"A collection name needs at least {COLLECTION_NAME_MIN} characters."
    if len(text) > COLLECTION_NAME_MAX:
        return f"A collection name can be at most {COLLECTION_NAME_MAX} characters."
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return "A collection name can't contain control characters."
    return ""


def _short_tag(name: str, mod_id: int = 0, file_id: int = 0, md5: str = "") -> str:
    """Reference tag identifying the exact FILE this entry pins — Vortex's
    matcher treats an installed mod carrying the same referenceTag as already
    satisfying a rule without checking anything else, so the tag must change
    whenever the file does."""
    parts = [p for p in (str(mod_id or ""), str(file_id or ""), md5 or "") if p]
    identity = "|".join(parts) or name
    return hashlib.md5(identity.encode("utf-8")).hexdigest()[:10]


def _read_row_meta(staging_root, name: str):
    if not staging_root:
        return None
    meta_path = Path(staging_root) / name / "meta.ini"
    if not meta_path.is_file():
        return None
    try:
        from Nexus.nexus_meta import read_meta
        return read_meta(meta_path)
    except Exception:
        return None


_fileid_index_cache: dict = {}


def _fileid_archive_index(directory: Path) -> dict:
    try:
        mtime = directory.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(directory)
    cached = _fileid_index_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    index: dict = {}
    try:
        for sidecar in directory.glob("*.fileid"):
            try:
                file_id = int(sidecar.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            archive = sidecar.with_suffix("")
            if file_id and archive.is_file():
                index[file_id] = archive
    except OSError:
        return {}
    _fileid_index_cache[key] = (mtime, index)
    return index


def _cached_archive(meta, game_name: str = "") -> "Path | None":
    """The mod's original archive in the download cache, or None."""
    if meta is None:
        return None
    root = get_download_cache_dir()
    dirs = ([root / game_name] if game_name else []) + [root]
    fname = (getattr(meta, "installation_file", "") or "").strip()
    if fname:
        for directory in dirs:
            path = directory / fname
            if path.is_file():
                return path
    file_id = int(getattr(meta, "file_id", 0) or 0)
    if file_id:
        for directory in dirs:
            hit = _fileid_archive_index(directory).get(file_id)
            if hit is not None:
                return hit
    return None


def _archive_md5(archive: Path) -> str:
    try:
        from Nexus.nexus_download import _md5_cache_get, _md5_cache_put
    except Exception:
        _md5_cache_get = _md5_cache_put = None
    if _md5_cache_get is not None:
        cached = _md5_cache_get(archive)
        if cached:
            return cached
    md5 = hashlib.md5()
    try:
        with open(archive, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                md5.update(chunk)
    except OSError:
        return ""
    digest = md5.hexdigest()
    if _md5_cache_put is not None:
        try:
            _md5_cache_put(archive, digest)
        except Exception:
            pass
    return digest


_FILENAME_ILLEGAL_RE = None


def _safe_archive_component(name: str) -> str:
    global _FILENAME_ILLEGAL_RE
    if _FILENAME_ILLEGAL_RE is None:
        import re
        _FILENAME_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    safe = _FILENAME_ILLEGAL_RE.sub("", name or "").strip(". ")
    return safe or "mod"


def _bundled_folder_name(mod_name: str, version: str = "") -> str:
    """Vortex's bundled-folder name: "Bundled - <mod name> (v<version>)"."""
    label = (mod_name or "mod").strip()
    version = (version or "").strip()
    if version:
        label = f"{label} (v{version})"
    safe = _safe_archive_component(f"Bundled - {label}")
    return safe.rstrip(". ") or "Bundled - mod"


# ---------------------------------------------------------------------------
# Plugins + LOOT rules (Bethesda-family profiles)
# ---------------------------------------------------------------------------

def _plugins_block(profile_dir) -> "list | None":
    path = Path(profile_dir) / "plugins.txt" if profile_dir else None
    if not path or not path.is_file():
        return None
    try:
        from Utils.plugins.plugins import read_plugins
        entries = read_plugins(path)
    except Exception:
        return None
    return [{"name": e.name.lower(), "enabled": bool(e.enabled)}
            for e in entries if getattr(e, "name", "")]


def _plugin_rules_block(profile_dir, known_plugins=None) -> "dict | None":
    """The profile's LOOT userlist.yaml as the manifest's pluginRules — the
    exact inverse of collection_reset._apply_collection_groups."""
    path = Path(profile_dir) / "userlist.yaml" if profile_dir else None
    if not path or not path.is_file():
        return None
    try:
        from Utils.userlist import parse_userlist
        data = parse_userlist(path)
    except Exception:
        return None

    def _names(items) -> list:
        out = []
        for it in items or []:
            name = it.get("name") if isinstance(it, dict) else it
            if name:
                out.append(str(name).lower())
        return out

    plugins_out = []
    for entry in data.get("plugins", []):
        name = (entry.get("name") or "").lower()
        if not name:
            continue
        if known_plugins is not None and name not in known_plugins:
            continue
        rule: dict = {"name": name}
        for field in ("after", "before"):
            values = _names(entry.get(field))
            if values:
                rule[field] = values
        if entry.get("group"):
            rule["group"] = entry["group"]
        if len(rule) > 1:
            plugins_out.append(rule)

    groups_out = []
    for grp in data.get("groups", []):
        gname = grp.get("name") if isinstance(grp, dict) else None
        if not gname:
            continue
        gout: dict = {"name": gname}
        after = [g for g in (grp.get("after") or []) if g]
        if after:
            gout["after"] = after
        groups_out.append(gout)

    if not plugins_out and not groups_out:
        return None
    rules: dict = {"plugins": plugins_out}
    if groups_out:
        rules["groups"] = groups_out
    return rules


# ---------------------------------------------------------------------------
# modRules — reuse the original collection's rules when we still have them,
# else a minimal adjacent-mod chain (enough to fully constrain the topo-sort
# back to the current order on reimport, without an O(n^2) pairwise blowup).
# ---------------------------------------------------------------------------

def build_mod_rules(ordered_logical_names: list, profile_dir) -> list:
    """*ordered_logical_names* — manifest mod ``name`` values, HIGHEST
    priority first (matches ``load_rows``/``rows`` order — the modlist's own
    top-to-bottom order)."""
    original = read_profile_manifest(profile_dir).get("modRules") or []
    names = set(ordered_logical_names)
    if original:
        reused = []
        for rule in original:
            ref = ((rule.get("reference") or {}).get("logicalFileName")
                   or (rule.get("reference") or {}).get("logicalFilename"))
            src = ((rule.get("source") or {}).get("logicalFileName")
                   or (rule.get("source") or {}).get("logicalFilename"))
            if ref in names and src in names:
                reused.append(rule)
        if reused:
            return reused

    rules = []
    for higher, lower in zip(ordered_logical_names, ordered_logical_names[1:]):
        rules.append({
            "type": "after",
            "source": {"logicalFileName": higher},
            "reference": {"logicalFileName": lower},
        })
    return rules


# ---------------------------------------------------------------------------
# Manifest build
# ---------------------------------------------------------------------------

def build_collection_manifest(rows, game, info: dict, *,
                              progress_cb=None) -> "tuple[dict, list, list]":
    """Build (manifest, bundle_jobs, warnings) from export rows for *game*.

    *info* supplies the manifest info block: name, author, authorUrl,
    description, gameVersions (list), recommendNewProfile,
    excludePluginRules. *rows* are export rows in modlist priority order
    (highest priority first) from Utils.profile.profile_export.load_rows.
    """
    progress_cb = progress_cb or (lambda *_a: None)
    staging_root = game.get_effective_mod_staging_path() if game else None
    profile_dir = getattr(game, "_active_profile_dir", None) if game else None
    game_name = getattr(game, "name", "") or ""
    game_domain = normalise_game_domain(
        getattr(game, "nexus_game_domain", "") or "")

    warnings: list = []
    mods: list = []
    bundle_jobs: list = []
    logical_names: list = []   # exported, enabled mods, priority order

    active = [r for r in rows if r.get("source") != "ignore"]
    skipped_disabled = [r["name"] for r in active if r.get("enabled") is False]
    if skipped_disabled:
        warnings.append(
            f"{len(skipped_disabled)} disabled mod(s) were left out of the "
            "collection (Nexus collections have no disabled state).")
        active = [r for r in active if r.get("enabled") is not False]

    total = len(active)
    for i, row in enumerate(active):
        name = row["name"]
        progress_cb(i, total, PHASE_META)
        meta = _read_row_meta(staging_root, name)
        archive = _cached_archive(meta, game_name)
        version = row.get("version") or (getattr(meta, "version", "") or "")
        author = (getattr(meta, "author", "") or "").strip()
        row_source = row.get("source", "nexus")
        mod_domain = (normalise_game_domain(getattr(meta, "game_domain", "") or "")
                      or normalise_game_domain(row.get("game_domain") or "")
                      or game_domain)

        if row_source == "bundle" and row.get("mod_id") and row.get("file_id"):
            warnings.append(
                f"'{name}': can't be bundled because it is available on "
                "Nexus — exported as a normal Nexus download instead.")
            row_source = "nexus"

        policy = row.get("update_policy") or "exact"
        if policy not in UPDATE_POLICIES or row_source == "bundle":
            policy = "exact"

        md5 = ""
        file_size = row.get("size_bytes") or (getattr(meta, "file_size", 0) or 0)

        if row_source == "bundle":
            progress_cb(i, total, PHASE_BUNDLE)
            mod_dir = Path(staging_root) / name if staging_root else None
            if not mod_dir or not mod_dir.is_dir():
                warnings.append(f"'{name}': staged files not found — skipped.")
                continue
            bundle_folder = _bundled_folder_name(name, version)
            file_size = 0
            member_count = 0
            for fp in sorted(mod_dir.rglob("*")):
                if not fp.is_file() or fp.name == "meta.ini":
                    continue
                rel = fp.relative_to(mod_dir).as_posix()
                bundle_jobs.append((fp, f"bundled/{bundle_folder}/{rel}"))
                try:
                    file_size += fp.stat().st_size
                except OSError:
                    pass
                member_count += 1
            if not member_count:
                warnings.append(f"'{name}': no files to bundle — skipped.")
                continue
            tag = _short_tag(name, md5=f"bundle:{bundle_folder}:{file_size}")
            source: dict = {
                "type": "bundle",
                "fileSize": file_size,
                "logicalFilename": f"{bundle_folder}.7z",
                "updatePolicy": "exact",
                "tag": tag,
                "fileExpression": bundle_folder,
            }
        else:
            if archive is not None:
                progress_cb(i, total, PHASE_HASH)
                md5 = _archive_md5(archive)
                if not file_size:
                    file_size = archive.stat().st_size
            tag = _short_tag(name, row.get("mod_id") or 0, row.get("file_id") or 0,
                             md5 or row.get("direct_url", ""))
            if row_source == "direct":
                source = {"type": "direct", "url": row.get("direct_url", "")}
            elif row_source == "browse":
                source = {"type": "browse"}
                if row.get("direct_url"):
                    source["url"] = row["direct_url"]
            else:
                source = {
                    "type": "nexus",
                    "modId": row["mod_id"],
                    "fileId": row["file_id"],
                    "logicalFilename": name,
                }
            if md5:
                source["md5"] = md5
            if file_size:
                source["fileSize"] = file_size
            if row_source != "browse":
                source["updatePolicy"] = policy
            source["tag"] = tag

        if row.get("instructions"):
            source["instructions"] = row["instructions"]

        mod_entry: dict = {
            "name": name,
            "version": version or "",
            "optional": bool(row.get("optional")),
            "domainName": mod_domain,
            "source": source,
        }
        if row.get("root_folder"):
            mod_entry["details"] = {"type": "dinput"}
        if author:
            mod_entry["author"] = author
        if row.get("has_fomod"):
            choices = resolve_installer_choices(row, game_name, profile_dir)
            if choices is not None:
                mod_entry["choices"] = choices
            else:
                warnings.append(
                    f"'{name}': installer choices could not be found to "
                    "export — users will be asked to choose interactively.")

        logical_names.append(name)
        mods.append(mod_entry)

    manifest: dict = {
        "info": {
            "author": info.get("author", ""),
            "authorUrl": info.get("authorUrl", ""),
            "name": info.get("name", ""),
            "description": info.get("description", ""),
            "installInstructions": info.get("installInstructions", ""),
            "domainName": game_domain,
            "gameVersions": list(info.get("gameVersions") or []),
        },
        "mods": mods,
        "modRules": build_mod_rules(logical_names, profile_dir),
        "collectionConfig": {
            "recommendNewProfile": bool(info.get("recommendNewProfile", True)),
            "excludePluginRules": bool(info.get("excludePluginRules", False)),
        },
    }

    plugins = _plugins_block(profile_dir)
    if plugins is not None:
        manifest["plugins"] = plugins
        plugin_rules = _plugin_rules_block(
            profile_dir, {p["name"] for p in plugins})
        if plugin_rules is not None:
            manifest["pluginRules"] = plugin_rules

    return manifest, bundle_jobs, warnings


# ---------------------------------------------------------------------------
# Archive write
# ---------------------------------------------------------------------------

def _sample_compressibility(bundle_jobs) -> float:
    import lzma
    jobs = list(bundle_jobs or ())
    if not jobs:
        return 0.0
    step = max(1, len(jobs) // _SAMPLE_FILES)
    picks = jobs[::step][:_SAMPLE_FILES]
    per_file = max(64 * 1024, _SAMPLE_BYTES // max(1, len(picks)))
    raw = bytearray()
    for src, _arc in picks:
        try:
            with open(src, "rb") as fh:
                raw += fh.read(per_file)
        except OSError:
            continue
        if len(raw) >= _SAMPLE_BYTES:
            break
    if not raw:
        return 0.0
    packed = lzma.compress(
        bytes(raw), format=lzma.FORMAT_RAW,
        filters=[{"id": lzma.FILTER_LZMA2, "preset": 1}])
    return len(packed) / len(raw)


def _pack_filters(payload_bytes: int, bundle_jobs=None) -> "tuple[list | None, str]":
    """The py7zr filter chain to pack with (None = default), and why. Bundle
    payloads are usually already-compressed tool output (BC-compressed DDS,
    packed BSAs), where storing instead of compressing turns hours into
    seconds; a genuinely compressible payload still gets LZMA2 at a cheap
    preset, since archive size counts against the single-upload limit."""
    if payload_bytes < FAST_PACK_THRESHOLD:
        return None, ""
    import lzma
    ratio = _sample_compressibility(bundle_jobs)
    if ratio >= INCOMPRESSIBLE_RATIO:
        import py7zr
        return ([{"id": py7zr.FILTER_COPY}],
                f"storing {format_bytes(payload_bytes)} uncompressed — a "
                f"sample compressed to {ratio * 100:.0f}% of its size")
    return ([{"id": lzma.FILTER_LZMA2, "preset": 1}],
            f"packing {format_bytes(payload_bytes)} at LZMA2 preset 1")


def pack_collection(out_path, manifest: dict, bundle_jobs, *,
                    progress_cb=None, log_fn=None) -> Path:
    """Write collection.json + bundled files into a .7z; returns the final path."""
    import py7zr

    log_fn = log_fn or (lambda *_a: None)
    out_path = Path(out_path)
    if out_path.suffix.lower() != ".7z":
        out_path = out_path.with_suffix(".7z")
    progress_cb = progress_cb or (lambda *_a: None)

    payload = json.dumps(manifest, indent=1).encode("utf-8")
    sizes = [len(payload)]
    for src, _arc in bundle_jobs:
        try:
            sizes.append(Path(src).stat().st_size)
        except OSError:
            sizes.append(0)
    total = sum(sizes)
    done = 0
    progress_cb(0, total, PHASE_PACK)

    # Build beside the destination and rename on success: writing straight to
    # the final path leaves a truncated .7z looking like a real export if
    # anything fails part-way.
    part_path = out_path.with_name(out_path.name + ".part")
    try:
        with tempfile.TemporaryDirectory() as td:
            cj_path = Path(td) / "collection.json"
            cj_path.write_bytes(payload)
            filters, why = _pack_filters(total, bundle_jobs)
            if why:
                log_fn(why.capitalize() + ".")
            with py7zr.SevenZipFile(str(part_path), mode="w",
                                    filters=filters) as arc:
                arc.write(str(cj_path), "collection.json")
                done += sizes[0]
                progress_cb(done, total, PHASE_PACK)
                for (src, arcname), size in zip(bundle_jobs, sizes[1:]):
                    arc.write(str(Path(src)), arcname)
                    done += size
                    progress_cb(done, total, PHASE_PACK)
        os.replace(part_path, out_path)
    except BaseException:
        try:
            part_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return out_path


def export_collection(out_path, rows, game, info: dict, *,
                      progress_cb=None, log_fn=None) -> "tuple[Path, list]":
    """Build the manifest and write the archive; returns (final_path, warnings)."""
    manifest, bundle_jobs, warnings = build_collection_manifest(
        rows, game, info, progress_cb=progress_cb)
    if not manifest["mods"]:
        raise ValueError(
            "No exportable mods (all ignored, disabled, or missing).")
    final = pack_collection(out_path, manifest, bundle_jobs,
                            progress_cb=progress_cb, log_fn=log_fn)
    return final, warnings
