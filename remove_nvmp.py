#!/usr/bin/env python3
"""
NVMP remover for Fallout New Vegas (Windows-focused).

GOAL:
- Remove NVMP (Fallout New Vegas Multiplayer) artifacts cleanly while avoiding other mods.
- Works for manual installs and many mod-manager installs by scanning common locations.
- Removes only items strongly identified as NVMP by name/signature patterns.
- Edits load lists (plugins.txt/loadorder.txt) to remove NVMP lines.
- Default behavior: moves removed items to a timestamped backup folder (reversible).
- Optional: --delete for permanent deletion.

IMPORTANT:
- This is a best-effort remover. If NVMP was installed under non-NVMP names, it may not catch everything.
- Run terminal as Administrator if your game is under Program Files (x86).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# -----------------------------
# Identification heuristics
# -----------------------------
# Strong name patterns: keep conservative to avoid touching other mods.
NVMP_NAME_PATTERNS = [
    re.compile(r"\bnvmp\b", re.IGNORECASE),
    re.compile(r"nvmp_", re.IGNORECASE),
    re.compile(r"new\s*vegas\s*mp", re.IGNORECASE),
    re.compile(r"newvegasmp", re.IGNORECASE),
    re.compile(r"new\s*vegas\s*multiplayer", re.IGNORECASE),
]

# Extra known-ish filenames (helps if someone renames files partially)
NVMP_KNOWN_FILENAMES = {
    "nvmp_launcher.exe",
    "nvmp_start.exe",
    "nvmp_storyserver.exe",
    "nvmp.log",
    "nvmp_launcher_last_error.log",
}

# Load order / plugin list files (vanilla + mod managers)
LOADLIST_FILENAMES = ("plugins.txt", "loadorder.txt")

# INI files where NVMP sometimes leaves lines/sections (we only remove lines containing NVMP markers)
INI_FILENAMES = ("Fallout.ini", "FalloutPrefs.ini", "nvse_config.ini", "nvse.ini")

# Safety cap
DEFAULT_MAX_MATCHES = 10000


def is_windows() -> bool:
    return os.name == "nt"


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def pat_match(name: str) -> bool:
    if name.lower() in NVMP_KNOWN_FILENAMES:
        return True
    return any(p.search(name) for p in NVMP_NAME_PATTERNS)


def resolve_path(p: Optional[str]) -> Optional[Path]:
    if not p:
        return None
    return Path(p).expanduser().resolve()


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


# -----------------------------
# Game detection (Steam/GOG + registry + steam libraryfolders)
# -----------------------------
def detect_from_registry() -> List[Path]:
    out: List[Path] = []
    if not is_windows():
        return out
    try:
        import winreg  # type: ignore

        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Bethesda Softworks\FalloutNV", "Installed Path"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Bethesda Softworks\FalloutNV", "Installed Path"),
        ]
        for hive, key_path, value_name in keys:
            try:
                with winreg.OpenKey(hive, key_path) as k:
                    val, _ = winreg.QueryValueEx(k, value_name)
                p = Path(val)
                if (p / "FalloutNV.exe").exists():
                    out.append(p.resolve())
            except OSError:
                continue
    except Exception:
        pass
    return out


def detect_from_steam_libraryfolders() -> List[Path]:
    out: List[Path] = []
    if not is_windows():
        return out

    pf86 = os.environ.get("ProgramFiles(x86)", "")
    steam_root = Path(pf86) / "Steam"
    lib_vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    if not lib_vdf.exists():
        return out

    try:
        txt = lib_vdf.read_text(encoding="utf-8", errors="ignore")
        paths = re.findall(r'"\s*path\s*"\s*"([^"]+)"', txt, flags=re.IGNORECASE)
        for raw in paths:
            lib = Path(raw)
            cand = lib / "steamapps" / "common" / "Fallout New Vegas"
            if (cand / "FalloutNV.exe").exists():
                out.append(cand.resolve())
    except Exception:
        pass
    return out


def detect_common_locations() -> List[Path]:
    out: List[Path] = []
    pf86 = os.environ.get("ProgramFiles(x86)")
    pf = os.environ.get("ProgramFiles")

    candidates: List[Path] = []
    if pf86:
        candidates += [
            Path(pf86) / "Steam" / "steamapps" / "common" / "Fallout New Vegas",
            Path(pf86) / "GOG Galaxy" / "Games" / "Fallout New Vegas",
        ]
    if pf:
        candidates += [
            Path(pf) / "Steam" / "steamapps" / "common" / "Fallout New Vegas",
            Path(pf) / "GOG Galaxy" / "Games" / "Fallout New Vegas",
        ]

    for c in candidates:
        if (c / "FalloutNV.exe").exists():
            out.append(c.resolve())
    return out


def autodetect_game_dir() -> Optional[Path]:
    found: List[Path] = []
    for f in (detect_from_registry(), detect_from_steam_libraryfolders(), detect_common_locations()):
        found.extend(f)

    # Deduplicate
    dedup = []
    seen = set()
    for p in found:
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            dedup.append(p)

    return dedup[0] if dedup else None


# -----------------------------
# Scan roots (game + user + mod managers)
# -----------------------------
def user_dirs() -> List[Path]:
    dirs: List[Path] = []
    up = os.environ.get("USERPROFILE")
    local = os.environ.get("LOCALAPPDATA")
    roam = os.environ.get("APPDATA")

    if up:
        dirs += [
            Path(up) / "Documents" / "My Games" / "FalloutNV",
            Path(up) / "Documents" / "My Games" / "Fallout New Vegas",
        ]
    if local:
        dirs += [
            Path(local) / "FalloutNV",
            Path(local) / "Fallout New Vegas",
            Path(local) / "Bethesda Softworks" / "FalloutNV",
        ]
    if roam:
        # Vortex commonly stores game-specific stuff here
        dirs += [
            Path(roam) / "Vortex" / "falloutnv",
            Path(roam) / "Vortex" / "fallout new vegas",
            Path(roam) / "Vortex",
        ]
    return [d for d in dirs if d.exists()]


def likely_vortex_mod_roots() -> List[Path]:
    # Vortex "mods" folder is usually under %APPDATA%\Vortex\<game>\mods
    roots: List[Path] = []
    roam = os.environ.get("APPDATA")
    if roam:
        for g in ("falloutnv", "fallout new vegas"):
            cand = Path(roam) / "Vortex" / g / "mods"
            if cand.exists():
                roots.append(cand)
    return roots


def likely_mo2_roots_near_game(game_dir: Optional[Path]) -> List[Path]:
    # MO2 can be anywhere; this is best-effort only.
    roots: List[Path] = []
    if not game_dir:
        return roots
    # Sometimes MO2 is in a sibling folder or nearby user-chosen location. We won't guess too hard.
    # But we can scan the game dir parents for a "Mod Organizer 2" folder.
    for parent in [game_dir.parent, game_dir.parent.parent]:
        if not parent.exists():
            continue
        mo2 = parent / "Mod Organizer 2"
        if mo2.exists():
            roots.append(mo2)
    return roots


def build_roots(game_dir: Optional[Path], mo2_dir: Optional[Path], vortex_dir: Optional[Path]) -> List[Path]:
    roots: List[Path] = []

    if game_dir:
        roots += [
            game_dir,
            game_dir / "Data",
            game_dir / "Data" / "nvse" / "plugins",
        ]

    roots += user_dirs()

    # Vortex: use explicit root if provided, else common defaults
    if vortex_dir:
        roots.append(vortex_dir)
    else:
        roots += likely_vortex_mod_roots()

    # MO2: use explicit root if provided, else try nearby
    if mo2_dir:
        roots += [
            mo2_dir,
            mo2_dir / "mods",
            mo2_dir / "overwrite",
            mo2_dir / "profiles",
        ]
    else:
        for mo2 in likely_mo2_roots_near_game(game_dir):
            roots += [
                mo2,
                mo2 / "mods",
                mo2 / "overwrite",
                mo2 / "profiles",
            ]

    # Existing only, dedupe
    out: List[Path] = []
    seen = set()
    for r in roots:
        try:
            rr = r.resolve()
        except Exception:
            rr = r
        key = str(rr).lower()
        if key in seen:
            continue
        if rr.exists():
            seen.add(key)
            out.append(rr)
    return out


# -----------------------------
# Find NVMP items
# -----------------------------
def walk_find_matches(roots: Iterable[Path], max_matches: int) -> List[Path]:
    matches: List[Path] = []
    seen = set()

    def add(p: Path) -> None:
        nonlocal matches
        key = str(p).lower()
        if key not in seen:
            seen.add(key)
            matches.append(p)

    for root in roots:
        # Walk without following symlinks (default)
        for dirpath, dirnames, filenames in os.walk(root):
            dp = Path(dirpath)

            # check dirs by name
            for d in list(dirnames):
                if pat_match(d):
                    add((dp / d).resolve())
                    if len(matches) >= max_matches:
                        return matches

            # check files by name
            for f in filenames:
                if pat_match(f):
                    add((dp / f).resolve())
                    if len(matches) >= max_matches:
                        return matches

    return sorted(matches, key=lambda p: str(p).lower())


def collapse_covered_paths(paths: List[Path]) -> List[Path]:
    # If a directory is selected, remove its children from the list.
    path_set = set(paths)
    out: List[Path] = []
    for p in sorted(paths, key=lambda x: (len(x.parts), str(x).lower())):
        covered = any(parent in path_set for parent in p.parents)
        if not covered:
            out.append(p)
    return out


# -----------------------------
# Backup + removal
# -----------------------------
def backup_root(default_base: Path) -> Path:
    return (default_base / f"NVMP_Removed_{stamp()}").resolve()


def safe_rel_for_backup(p: Path) -> Path:
    # Avoid ':' in Windows paths inside backup folder
    return Path(str(p).replace(":", ""))


def remove_path(p: Path, backup_dir: Optional[Path], permanent_delete: bool) -> Tuple[Path, str]:
    try:
        if not p.exists():
            return p, "SKIP (missing)"

        if permanent_delete:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)
            return p, "DELETED"

        if not backup_dir:
            return p, "ERROR (no backup dir set)"

        dst = backup_dir / safe_rel_for_backup(p)
        ensure_dir(dst.parent)
        shutil.move(str(p), str(dst))
        return p, f"MOVED -> {dst}"

    except PermissionError:
        return p, "ERROR (permission denied; run as Administrator)"
    except Exception as e:
        return p, f"ERROR ({type(e).__name__}: {e})"


# -----------------------------
# Clean load lists + INIs
# -----------------------------
def strip_nvmp_lines_from_text_file(path: Path, backup_dir: Optional[Path], permanent_delete: bool) -> List[str]:
    msgs: List[str] = []
    try:
        if not path.exists() or not path.is_file():
            return msgs

        raw = path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
        kept: List[str] = []
        removed: List[str] = []

        for line in raw:
            if pat_match(line):
                removed.append(line.rstrip("\n"))
            else:
                kept.append(line)

        if not removed:
            return msgs

        # backup original file before edit
        if not permanent_delete and backup_dir:
            ensure_dir(backup_dir)
            shutil.copy2(str(path), str(backup_dir / f"{path.name}.bak"))
        elif permanent_delete and backup_dir:
            ensure_dir(backup_dir)
            shutil.copy2(str(path), str(backup_dir / f"{path.name}.bak"))

        path.write_text("".join(kept), encoding="utf-8")
        msgs.append(f"EDITED: {path} (removed {len(removed)} NVMP line(s))")
        return msgs

    except PermissionError:
        return [f"ERROR editing {path}: permission denied (run as Administrator)"]
    except Exception as e:
        return [f"ERROR editing {path}: {type(e).__name__}: {e}"]


def find_text_targets(roots: Iterable[Path]) -> Tuple[List[Path], List[Path]]:
    loadlists: List[Path] = []
    inis: List[Path] = []

    seen = set()
    for root in roots:
        for dirpath, _, filenames in os.walk(root):
            dp = Path(dirpath)
            # loadlists
            for lf in LOADLIST_FILENAMES:
                if lf in filenames:
                    p = (dp / lf).resolve()
                    k = str(p).lower()
                    if k not in seen:
                        seen.add(k)
                        loadlists.append(p)
            # ini files
            for ini in INI_FILENAMES:
                if ini in filenames:
                    p = (dp / ini).resolve()
                    k = str(p).lower()
                    if k not in seen:
                        seen.add(k)
                        inis.append(p)

    # Also include common Bethesda-style locations that might not be under roots
    # (but roots already includes those in user_dirs()).
    return sorted(loadlists, key=lambda p: str(p).lower()), sorted(inis, key=lambda p: str(p).lower())


# -----------------------------
# Main
# -----------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove NVMP (Fallout New Vegas Multiplayer) artifacts while avoiding other mods."
    )
    parser.add_argument("--game", type=str, default=None, help="Path to Fallout New Vegas game directory.")
    parser.add_argument("--mo2", type=str, default=None, help="Path to Mod Organizer 2 base directory (optional).")
    parser.add_argument("--vortex", type=str, default=None, help="Path to Vortex mods/staging directory (optional).")
    parser.add_argument("--backup-dir", type=str, default=None, help="Where to store backups (default: current dir).")
    parser.add_argument("--delete", action="store_true", help="Permanently delete instead of backing up.")
    parser.add_argument("--max-matches", type=int, default=DEFAULT_MAX_MATCHES, help="Safety cap for matches.")
    args = parser.parse_args()

    if not is_windows():
        print("Warning: This script is intended for Windows. Continuing anyway.\n")

    game_dir = resolve_path(args.game) if args.game else autodetect_game_dir()
    mo2_dir = resolve_path(args.mo2)
    vortex_dir = resolve_path(args.vortex)

    if args.game and game_dir and not (game_dir / "FalloutNV.exe").exists():
        print(f"ERROR: --game does not look like a Fallout New Vegas folder: {game_dir}")
        print("Expected FalloutNV.exe in that directory.")
        return 2

    if not game_dir:
        print("ERROR: Could not auto-detect Fallout New Vegas install folder.")
        print('Run again with: --game "C:\\...\\Fallout New Vegas"')
        return 2

    roots = build_roots(game_dir, mo2_dir, vortex_dir)

    # Backup folder location
    if args.delete:
        backup_dir = None
    else:
        base = resolve_path(args.backup_dir) if args.backup_dir else Path.cwd()
        backup_dir = backup_root(base)
        ensure_dir(backup_dir)

    print("=" * 80)
    print("NVMP REMOVER")
    print(f"Game dir: {game_dir}")
    print(f"Mode: {'PERMANENT DELETE' if args.delete else 'BACKUP+REMOVE'}")
    if backup_dir:
        print(f"Backup folder: {backup_dir}")
    print("\nScan roots:")
    for r in roots:
        print(f"  - {r}")
    print("=" * 80)

    # 1) Find NVMP-like files/folders
    matches = walk_find_matches(roots, max_matches=args.max_matches)
    matches = collapse_covered_paths(matches)

    # 2) Find text targets to clean (plugins/loadorder + ini files)
    loadlists, inis = find_text_targets(roots)

    # Remove NVMP lines from loadlists and ini files (safe, line-based)
    edit_msgs: List[str] = []
    for p in loadlists:
        edit_msgs += strip_nvmp_lines_from_text_file(p, backup_dir=backup_dir, permanent_delete=args.delete)
    for p in inis:
        # only removes lines that contain NVMP markers; keeps rest
        edit_msgs += strip_nvmp_lines_from_text_file(p, backup_dir=backup_dir, permanent_delete=args.delete)

    # 3) Remove matched files/folders (move to backup or delete)
    if not matches and not edit_msgs:
        print("No NVMP artifacts found by signature scan, and no NVMP lines found in text files.")
        return 0

    if matches:
        print(f"\nRemoving {len(matches)} NVMP artifact(s):")
        results: List[Tuple[Path, str]] = []
        for p in matches:
            res = remove_path(p, backup_dir=backup_dir, permanent_delete=args.delete)
            results.append(res)
            print(f"  - {res[0]} -> {res[1]}")
    else:
        print("\nNo NVMP-named files/folders found to remove.")

    if edit_msgs:
        print("\nCleaned NVMP entries from text files:")
        for m in edit_msgs:
            print(f"  - {m}")

    print("\nDONE.")
    if backup_dir and not args.delete:
        print(f"If you need to restore, your removed files are here:\n  {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
