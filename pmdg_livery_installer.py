#!/usr/bin/env python3
"""Install MSFS 2024-native PMDG liveries from a ZIP or folder.

The module deliberately keeps the installer engine independent of Tk so it can
be tested and used from the command line.  The desktop UI is only a thin
drag-and-drop front end around ``install_livery``.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable


APP_NAME = "PMDG Livery Drop Installer"
APP_DATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
SETTINGS_FILE = APP_DATA / "settings.json"
BACKUP_ROOT = APP_DATA / "layout-backups"
DIAGNOSTIC_LOG = APP_DATA / "installer.log"
MAX_ARCHIVE_FILES = 25_000
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024 * 1024
MAX_NESTED_ARCHIVE_DEPTH = 3

AIRCRAFT_NAMES = {
    "pmdg-aircraft-736": "PMDG 737-600",
    "pmdg-aircraft-737": "PMDG 737-700",
    "pmdg-aircraft-738": "PMDG 737-800",
    "pmdg-aircraft-739": "PMDG 737-900",
    "pmdg-aircraft-77w": "PMDG 777-300ER",
    "pmdg-aircraft-77f": "PMDG 777F",
    "pmdg-aircraft-77er": "PMDG 777-200ER",
    "pmdg-aircraft-77l": "PMDG 777-200LR",
}
AIRCRAFT_FOLDER_TO_PRODUCT = {name.casefold(): package for package, name in AIRCRAFT_NAMES.items()}


class InstallError(RuntimeError):
    """A clear, non-programming error that can be shown to an end user."""


@dataclass(frozen=True)
class Product:
    """A PMDG product found immediately inside a Community folder."""

    base_name: str
    community: Path

    @property
    def base_package(self) -> Path:
        return self.community / self.base_name

    @property
    def livery_package(self) -> Path:
        return self.community / f"{self.base_name}-liveries"

    @property
    def display_name(self) -> str:
        title = AIRCRAFT_NAMES.get(self.base_name, self.base_name)
        return f"{title}  [{self.base_name.removeprefix('pmdg-aircraft-')}]"


@dataclass(frozen=True)
class InstallResult:
    """Useful, user-safe information about one completed install."""

    product: Product
    livery_package: Path
    destinations: tuple[Path, ...]
    copied_files: int
    layout_entries: int
    layout_backup: Path | None


@dataclass(frozen=True)
class LiveryDetection:
    """Aircraft inferred from livery package internals, never just its filename."""

    candidates: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def product_name(self) -> str | None:
        return self.candidates[0] if len(self.candidates) == 1 else None

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidates) > 1


def normalized(path: str | Path) -> Path:
    """Return an absolute lexical path without requiring it to exist."""

    return Path(path).expanduser().absolute()


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def is_reparse_point(path: Path) -> bool:
    """Detect a Windows link/junction without following it."""

    try:
        return path.is_symlink() or bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return path.is_symlink()


def load_settings() -> dict[str, object]:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data: dict[str, object]) -> None:
    APP_DATA.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(SETTINGS_FILE)


def write_diagnostic(message: str) -> None:
    """Keep the last troubleshooting details locally, never inside Community."""

    try:
        APP_DATA.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with DIAGNOSTIC_LOG.open("a", encoding="utf-8") as log:
            log.write(f"[{stamp}] {message}\n")
    except OSError:
        # Logging must never prevent a livery installation or visible error.
        pass


def parse_installed_packages_path(user_cfg: Path) -> Path | None:
    """Read the InstalledPackagesPath line without depending on its position."""

    try:
        content = user_cfg.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    match = re.search(r'^\s*InstalledPackagesPath\s+"([^"]+)"', content, re.MULTILINE | re.IGNORECASE)
    return normalized(match.group(1)) if match else None


def user_cfg_candidates() -> list[Path]:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    roaming = Path(os.environ.get("APPDATA", ""))
    candidates: list[Path] = []
    if local:
        packages = local / "Packages"
        if packages.is_dir():
            for limitless in packages.glob("Microsoft.Limitless_*"):
                candidates.extend(
                    [
                        limitless / "LocalCache" / "UserCfg.opt",
                        limitless / "LocalState" / "UserCfg.opt",
                    ]
                )
    if roaming:
        candidates.append(roaming / "Microsoft Flight Simulator 2024" / "UserCfg.opt")
    return candidates


def detect_community_folders() -> list[Path]:
    """Find Community/Community2024 from both MSFS 2024 distributions."""

    found: list[Path] = []
    for cfg in user_cfg_candidates():
        package_root = parse_installed_packages_path(cfg)
        if not package_root:
            continue
        for child_name in ("Community", "Community2024"):
            candidate = package_root / child_name
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)
    return found


def product_base_name(package_name: str) -> str | None:
    name = package_name.casefold()
    if name.endswith("-liveries"):
        name = name[: -len("-liveries")]
    # PMDG/OC3 may add individual livery packages beside the base aircraft
    # (for example ``pmdg-aircraft-738-aeroflot-ra-73099``).  They are a
    # livery source, not an aircraft target, so exposing them in the product
    # selector would silently send a drop to the wrong companion package.
    return name if name in AIRCRAFT_NAMES else None


def discover_products(community: str | Path) -> list[Product]:
    community_path = normalized(community)
    if not community_path.is_dir():
        return []
    names: set[str] = set()
    for child in community_path.iterdir():
        if child.is_dir():
            base = product_base_name(child.name)
            if base:
                names.add(base)
    return [Product(name, community_path) for name in sorted(names)]


def product_by_name(community: str | Path, requested_name: str) -> Product:
    requested = product_base_name(requested_name)
    if not requested:
        raise InstallError("The product must look like pmdg-aircraft-738 or pmdg-aircraft-77w.")
    matches = [item for item in discover_products(community) if item.base_name == requested]
    if not matches:
        raise InstallError(f"PMDG product not found in Community: {requested}")
    return matches[0]


def _record_product_evidence(
    records: dict[str, set[str]],
    product_name: str,
    detail: str,
) -> None:
    if product_name in AIRCRAFT_NAMES:
        records.setdefault(product_name, set()).add(detail)


def _inspect_livery_path(member_name: str, records: dict[str, set[str]]) -> None:
    """Infer an aircraft only from a package or airplane-folder path segment."""

    member = PurePosixPath(member_name.replace("\\", "/"))
    parts = member.parts
    folded_parts = [part.casefold() for part in parts]
    for part in folded_parts:
        for product_name in AIRCRAFT_NAMES:
            # A native companion package ends in -liveries; OC3 can also export
            # a self-contained livery package such as -738-airline-name.
            if part == product_name or part.startswith(f"{product_name}-"):
                _record_product_evidence(records, product_name, f"package path: {member_name}")
    for index, part in enumerate(folded_parts[:-1]):
        if part == "airplanes":
            product_name = AIRCRAFT_FOLDER_TO_PRODUCT.get(folded_parts[index + 1])
            if product_name:
                _record_product_evidence(records, product_name, f"airplane folder: {parts[index + 1]}")


def _inspect_livery_text(text: str, origin: str, records: dict[str, set[str]]) -> None:
    folded = text.casefold()
    for product_name in AIRCRAFT_NAMES:
        if product_name in folded:
            _record_product_evidence(records, product_name, f"configuration: {origin}")


def _detection_from_members(members: Iterable[tuple[str, str | None]]) -> LiveryDetection:
    records: dict[str, set[str]] = {}
    for member_name, configuration_text in members:
        _inspect_livery_path(member_name, records)
        if configuration_text:
            _inspect_livery_text(configuration_text, member_name, records)
    candidates = tuple(sorted(records))
    evidence = tuple(sorted(detail for group in records.values() for detail in group))
    return LiveryDetection(candidates, evidence)


def detect_livery_product(livery: str | Path, _nested_depth: int = 0) -> LiveryDetection:
    """Inspect a ZIP/folder's internal structure without extracting or copying it.

    Reliable evidence is limited to PMDG package names, aircraft folder names,
    and configuration references. A download filename is intentionally ignored.
    """

    input_path = normalized(livery)
    if not input_path.exists():
        raise InstallError(f"The livery does not exist: {input_path}")
    if input_path.is_file():
        if input_path.suffix.casefold() != ".zip":
            return LiveryDetection((), ())
        try:
            with zipfile.ZipFile(input_path) as archive:
                members: list[tuple[str, str | None]] = []
                embedded_archives: list[zipfile.ZipInfo] = []
                outer_has_livery_structure = False
                for entry in archive.infolist():
                    member = validate_zip_member(entry)
                    member_name = member.as_posix()
                    member_lower = member_name.casefold()
                    outer_has_livery_structure = outer_has_livery_structure or (
                        "simobjects/airplanes/" in member_lower
                        or member_lower.endswith("/livery.cfg")
                        or member_lower == "livery.cfg"
                        or member_lower.endswith("/livery.json")
                        or member_lower == "livery.json"
                    )
                    if not entry.is_dir() and member.suffix.casefold() == ".zip":
                        embedded_archives.append(entry)
                    configuration_text = None
                    if (
                        not entry.is_dir()
                        and entry.file_size <= 1_000_000
                        and member.name.casefold() in {"aircraft.cfg", "livery.cfg", "livery.json"}
                    ):
                        configuration_text = archive.read(entry).decode("utf-8-sig", errors="replace")
                    members.append((member_name, configuration_text))
                detection = _detection_from_members(members)
                if (
                    detection.candidates
                    or outer_has_livery_structure
                    or _nested_depth >= MAX_NESTED_ARCHIVE_DEPTH
                    or len(embedded_archives) != 1
                ):
                    return detection
                nested = embedded_archives[0]
                if nested.file_size > MAX_ARCHIVE_BYTES:
                    raise InstallError("Embedded ZIP is larger than 5 GB and was not inspected.")
                with tempfile.TemporaryDirectory(prefix="pmdg-livery-detect-") as scratch:
                    inner_archive = Path(scratch) / PurePosixPath(nested.filename).name
                    with archive.open(nested) as source, inner_archive.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                    return detect_livery_product(inner_archive, _nested_depth + 1)
        except zipfile.BadZipFile as exc:
            raise InstallError("That file is not a valid ZIP archive.") from exc

    members = []
    for current, directories, filenames in os.walk(input_path):
        directories[:] = [name for name in directories if not name.startswith(".")]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            relative = path.relative_to(input_path).as_posix()
            configuration_text = None
            if filename.casefold() in {"aircraft.cfg", "livery.cfg", "livery.json"}:
                try:
                    if path.stat().st_size <= 1_000_000:
                        configuration_text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    pass
            members.append((relative, configuration_text))
    return _detection_from_members(members)


def existing_aircraft_names(package: Path) -> list[str]:
    airplanes = package / "SimObjects" / "Airplanes"
    if not airplanes.is_dir():
        return []
    return sorted(child.name for child in airplanes.iterdir() if child.is_dir())


def aircraft_folder_for(product: Product) -> str:
    """Resolve the correct PMDG aircraft directory before copying a direct livery."""

    for package in (product.livery_package, product.base_package):
        names = existing_aircraft_names(package)
        if len(names) == 1:
            return names[0]
        known = AIRCRAFT_NAMES.get(product.base_name)
        if known and known in names:
            return known
    known = AIRCRAFT_NAMES.get(product.base_name)
    if known:
        return known
    raise InstallError(
        "Could not determine the PMDG airplane folder for this product. "
        "Use a product with an installed SimObjects/Airplanes folder first."
    )


def ensure_livery_package(product: Product) -> Path:
    """Create a minimal separate livery package; never modify the base aircraft."""

    destination = product.livery_package
    if destination.exists() and not destination.is_dir():
        raise InstallError(f"The livery package path is not a folder: {destination}")
    if destination.exists() and is_reparse_point(destination):
        raise InstallError(
            "The livery package is a symbolic link or junction. Installation is blocked "
            "to protect an MSFS Addons Linker source."
        )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "SimObjects" / "Airplanes").mkdir(parents=True, exist_ok=True)
    manifest = destination / "manifest.json"
    if not manifest.exists():
        title = f"{AIRCRAFT_NAMES.get(product.base_name, product.base_name)} liveries"
        manifest.write_text(
            json.dumps(
                {
                    "dependencies": [],
                    "content_type": "AIRCRAFT",
                    "title": title,
                    "manufacturer": "PMDG",
                    "creator": APP_NAME,
                    "package_version": "1.0.0",
                    "minimum_game_version": "1.0.0",
                    "release_notes": {"neutral": {"LastUpdate": "", "OlderHistory": ""}},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return destination


def validate_zip_member(info: zipfile.ZipInfo) -> PurePosixPath:
    member = PurePosixPath(info.filename.replace("\\", "/"))
    if not info.filename or member.is_absolute() or ".." in member.parts or ":" in member.parts[0]:
        raise InstallError(f"Unsafe path in ZIP: {info.filename!r}")
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise InstallError(f"ZIP contains a symbolic link, which is not accepted: {info.filename}")
    return member


def extract_zip_safely(archive: Path, destination: Path) -> None:
    """Extract after rejecting zip-slip, symlinks, and unlikely zip bombs."""

    try:
        with zipfile.ZipFile(archive) as zip_file:
            entries = zip_file.infolist()
            if len(entries) > MAX_ARCHIVE_FILES:
                raise InstallError(f"ZIP has too many files ({len(entries):,}).")
            total = sum(entry.file_size for entry in entries)
            if total > MAX_ARCHIVE_BYTES:
                raise InstallError("ZIP expands to more than 5 GB and was not extracted.")
            members = [(entry, validate_zip_member(entry)) for entry in entries]
            files_seen: set[str] = set()
            for entry, member in members:
                if not entry.is_dir():
                    canonical_name = member.as_posix().casefold()
                    if canonical_name in files_seen:
                        raise InstallError(f"ZIP contains the same file more than once: {entry.filename}")
                    files_seen.add(canonical_name)
                target = destination.joinpath(*member.parts)
                if not is_relative_to(target, destination):
                    raise InstallError(f"Unsafe ZIP target: {entry.filename!r}")
                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(entry) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
    except zipfile.BadZipFile as exc:
        raise InstallError("That file is not a valid ZIP archive.") from exc


def iter_directories(root: Path) -> Iterable[Path]:
    for current, directories, _ in os.walk(root):
        directories[:] = [name for name in directories if not name.startswith(".")]
        yield Path(current)


def livery_folder_like(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    if (folder / "livery.cfg").is_file():
        return True
    try:
        children = {child.name.casefold() for child in folder.iterdir() if child.is_dir()}
    except OSError:
        return False
    return any(name.startswith(("texture", "model", "panel")) for name in children)


def find_direct_livery_folders(source: Path) -> list[Path]:
    candidates = [folder for folder in iter_directories(source) if livery_folder_like(folder)]
    selected: list[Path] = []
    for candidate in sorted(candidates, key=lambda item: len(item.parts)):
        if not any(candidate.is_relative_to(parent) for parent in selected):
            selected.append(candidate)
    return selected


def find_package_source(source: Path) -> Path | None:
    for folder in sorted(iter_directories(source), key=lambda item: len(item.parts)):
        if folder.name.casefold().endswith("-liveries") and (folder / "SimObjects" / "Airplanes").is_dir():
            return folder
    return None


def find_simobjects_source(source: Path) -> Path | None:
    for folder in sorted(iter_directories(source), key=lambda item: len(item.parts)):
        if (folder / "SimObjects" / "Airplanes").is_dir():
            return folder
    return None


def source_has_installable_content(source: Path) -> bool:
    return bool(
        find_package_source(source)
        or find_simobjects_source(source)
        or find_direct_livery_folders(source)
    )


def nested_archives(source: Path) -> list[Path]:
    """List embedded ZIPs without following links or scanning hidden folders."""

    archives: list[Path] = []
    for current, directories, filenames in os.walk(source):
        directories[:] = [name for name in directories if not name.startswith(".")]
        current_path = Path(current)
        for filename in filenames:
            candidate = current_path / filename
            if candidate.suffix.casefold() == ".zip" and not is_reparse_point(candidate):
                archives.append(candidate)
    return sorted(archives, key=lambda item: item.as_posix().casefold())


def archive_has_livery_markers(archive: Path) -> bool:
    """Use central-directory names only; do not decompress an archive just to choose it."""

    try:
        with zipfile.ZipFile(archive) as zip_file:
            for entry in zip_file.infolist():
                member = validate_zip_member(entry).as_posix().casefold()
                if (
                    "simobjects/airplanes/" in member
                    or member.endswith("/livery.cfg")
                    or member == "livery.cfg"
                    or "pmdg-aircraft-" in member
                ):
                    return True
    except zipfile.BadZipFile as exc:
        raise InstallError(f"Embedded file is not a valid ZIP archive: {archive.name}") from exc
    return False


def unwrap_nested_archives(source: Path) -> Path:
    """Reach an installable livery inside common ZIP-in-ZIP download wrappers."""

    current = source
    for depth in range(MAX_NESTED_ARCHIVE_DEPTH + 1):
        if source_has_installable_content(current):
            return current
        if depth == MAX_NESTED_ARCHIVE_DEPTH:
            break
        archives = nested_archives(current)
        marked_archives = [archive for archive in archives if archive_has_livery_markers(archive)]
        choices = marked_archives or archives
        if not choices:
            return current
        if len(choices) > 1:
            names = ", ".join(archive.name for archive in choices[:5])
            raise InstallError(
                "The ZIP contains multiple possible livery archives. Extract the desired one first "
                f"or use a single-livery download. Found: {names}"
            )
        current = current / f"nested-livery-{depth + 1}"
        current.mkdir()
        extract_zip_safely(choices[0], current)
    return current


def copy_tree(source: Path, destination: Path, overwrite: bool) -> int:
    """Copy a tree without letting it follow link sources or overwrite by accident."""

    if is_reparse_point(source):
        raise InstallError(f"Source is a symbolic link/junction and was not copied: {source}")
    copied = 0
    for current, directories, filenames in os.walk(source):
        current_path = Path(current)
        if is_reparse_point(current_path):
            raise InstallError(f"Source contains a link/junction and was not copied: {current_path}")
        relative = current_path.relative_to(source)
        target_dir = destination / relative
        if target_dir.exists() and is_reparse_point(target_dir):
            raise InstallError(f"Destination contains a link/junction and was not written: {target_dir}")
        target_dir.mkdir(parents=True, exist_ok=True)
        for directory in directories:
            child = current_path / directory
            if is_reparse_point(child):
                raise InstallError(f"Source contains a link/junction and was not copied: {child}")
            (target_dir / directory).mkdir(exist_ok=True)
        for filename in filenames:
            input_file = current_path / filename
            output_file = target_dir / filename
            if output_file.exists() and is_reparse_point(output_file):
                raise InstallError(f"Destination contains a link/junction and was not written: {output_file}")
            if output_file.exists() and not overwrite:
                raise InstallError(f"A matching livery already exists: {output_file}")
            shutil.copy2(input_file, output_file)
            copied += 1
    return copied


def copy_package_content(source: Path, destination: Path, overwrite: bool) -> tuple[int, list[Path]]:
    """Copy a complete or SimObjects source, preserving its package hierarchy."""

    copied = 0
    destinations: list[Path] = []
    for item in source.iterdir():
        if item.name.casefold() in {"layout.json", "manifest.json"}:
            continue
        target = destination / item.name
        if item.is_dir():
            copied += copy_tree(item, target, overwrite)
        else:
            if target.exists() and not overwrite:
                raise InstallError(f"A matching package file already exists: {target}")
            shutil.copy2(item, target)
            copied += 1
        destinations.append(target)
    return copied, destinations


def layout_files(package: Path) -> Iterable[Path]:
    excluded_root = {"layout.json", "manifest.json"}
    for current, directories, filenames in os.walk(package):
        directories[:] = [name for name in directories if not name.startswith(".")]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            relative = path.relative_to(package)
            if len(relative.parts) == 1 and filename.casefold() in excluded_root:
                continue
            yield path


def filetime(path: Path) -> int:
    """Return the timestamp format used by MSFS package ``layout.json`` files.

    The package layout stores UTC ticks from the Unix epoch, not Windows
    FILETIME ticks.  The latter includes a 1601-to-1970 offset and makes the
    package appear to have invalidly distant modification dates to MSFS.
    """

    # ``st_mtime`` is a float and loses some 100-nanosecond precision for
    # contemporary dates. ``st_mtime_ns`` preserves the tick value exactly.
    return path.stat().st_mtime_ns // 100


def rebuild_layout(package: Path, product: Product) -> tuple[int, Path | None]:
    """Generate MSFS layout.json internally, avoiding an opaque external EXE."""

    layout = package / "layout.json"
    backup: Path | None = None
    if layout.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_package = re.sub(r"[^A-Za-z0-9_.-]", "_", product.base_name)
        backup = BACKUP_ROOT / safe_package / f"layout-{stamp}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(layout, backup)

    content = []
    total_size = 0
    for source in layout_files(package):
        relative = source.relative_to(package).as_posix()
        size = source.stat().st_size
        total_size += size
        content.append({"path": relative, "size": size, "date": filetime(source)})
    content.sort(key=lambda item: item["path"].casefold())
    layout.write_text(json.dumps({"content": content}, indent=2) + "\n", encoding="utf-8")

    manifest = package / "manifest.json"
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if isinstance(manifest_data, dict):
            manifest_data["total_package_size"] = str(total_size)
            manifest.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass
    return len(content), backup


def source_from_input(livery: Path, temp_root: Path) -> Path:
    livery = normalized(livery)
    if not livery.exists():
        raise InstallError(f"The livery does not exist: {livery}")
    if livery.is_dir():
        return unwrap_nested_archives(livery)
    if livery.suffix.casefold() == ".ptp":
        raise InstallError("PTP files are not supported. Use an MSFS 2024 ZIP exported for PMDG OC3.")
    if livery.suffix.casefold() != ".zip":
        raise InstallError("Drop a .zip livery or an extracted livery folder.")
    extracted = temp_root / "livery"
    extracted.mkdir()
    extract_zip_safely(livery, extracted)
    return unwrap_nested_archives(extracted)


def validate_install_bounds(source_input: Path, product: Product) -> None:
    source_input = normalized(source_input)
    if source_input.is_file():
        source_input = source_input.parent
    for target in (product.base_package, product.livery_package):
        if target.exists() and is_relative_to(source_input, target):
            raise InstallError("The livery source is already inside the selected PMDG package.")


def install_livery(
    livery: str | Path,
    product: Product,
    overwrite: bool = False,
    status_callback: Callable[[str], None] | None = None,
) -> InstallResult:
    """Install a compatible livery into a product-specific MSFS 2024 package."""

    def report(stage: str) -> None:
        if status_callback:
            status_callback(stage)

    livery_input = normalized(livery)
    if not product.community.is_dir():
        raise InstallError(f"Community folder no longer exists: {product.community}")
    validate_install_bounds(livery_input, product)

    with tempfile.TemporaryDirectory(prefix="pmdg-livery-") as scratch:
        report("Extracting and inspecting the livery ZIP…")
        source = source_from_input(livery_input, Path(scratch))
        source_detection = detect_livery_product(source)
        if source_detection.is_ambiguous:
            names = ", ".join(source_detection.candidates)
            raise InstallError(f"The livery contains multiple aircraft ({names}) and was not installed.")
        if not source_detection.product_name:
            raise InstallError(
                "Could not verify the PMDG aircraft from the livery contents, so nothing was installed. "
                "Use a native MSFS 2024 PMDG livery ZIP that includes livery.json, SimObjects, or a PMDG package folder."
            )
        if source_detection.product_name != product.base_name:
            expected = AIRCRAFT_NAMES[source_detection.product_name]
            raise InstallError(
                f"This livery identifies itself as {expected}, not {product.display_name}. Nothing was installed."
            )

        report(f"Preparing the {product.display_name} livery package…")
        destination = ensure_livery_package(product)
        if not is_relative_to(destination, product.community):
            raise InstallError("Refusing to write outside the selected Community folder.")
        package_source = find_package_source(source)
        simobjects_source = find_simobjects_source(source)
        copied = 0
        copied_destinations: list[Path] = []

        report("Copying livery files…")
        if package_source:
            source_base = product_base_name(package_source.name)
            if source_base and source_base != product.base_name:
                raise InstallError(
                    f"This ZIP is for {source_base}, not the selected {product.base_name} product."
                )
            copied, copied_destinations = copy_package_content(package_source, destination, overwrite)
        elif simobjects_source:
            simobjects = simobjects_source / "SimObjects"
            copied = copy_tree(simobjects, destination / "SimObjects", overwrite)
            copied_destinations = [destination / "SimObjects"]
        else:
            folders = find_direct_livery_folders(source)
            if not folders:
                raise InstallError(
                    "No MSFS 2024 PMDG livery structure was found. Expected a *-liveries "
                    "package, SimObjects folder, or a livery.cfg/texture folder."
                )
            livery_parent = destination / "SimObjects" / "Airplanes" / aircraft_folder_for(product) / "liveries" / "pmdg"
            for folder in folders:
                target = livery_parent / folder.name
                copied += copy_tree(folder, target, overwrite)
                copied_destinations.append(target)

    report("Rebuilding layout.json for MSFS 2024…")
    entries, backup = rebuild_layout(destination, product)
    report("Finalizing installation…")
    return InstallResult(product, destination, tuple(copied_destinations), copied, entries, backup)


def format_result(result: InstallResult) -> str:
    paths = "\n".join(f"• {path}" for path in result.destinations)
    backup = f"\nLayout backup: {result.layout_backup}" if result.layout_backup else ""
    return (
        f"Installed {result.copied_files:,} file(s) for {result.product.display_name}.\n\n"
        f"Destination:\n{paths}\n\nRebuilt layout.json with {result.layout_entries:,} entries.{backup}"
    )


def choose_initial_community() -> Path | None:
    saved = load_settings().get("community_folder")
    if isinstance(saved, str) and Path(saved).is_dir():
        return normalized(saved)
    candidates = detect_community_folders()
    if not candidates:
        return None
    return max(candidates, key=lambda path: len(discover_products(path)))


def run_cli(args: argparse.Namespace) -> int:
    if args.detect:
        found = detect_community_folders()
        if found:
            print("\n".join(str(path) for path in found))
            return 0
        print("No MSFS 2024 Community folder found.", file=sys.stderr)
        return 1
    if args.list_products or args.install:
        community = normalized(args.community) if args.community else choose_initial_community()
        if not community:
            print("No Community folder was supplied or detected.", file=sys.stderr)
            return 2
        if args.list_products:
            products = discover_products(community)
            print("\n".join(f"{product.base_name}\t{product.display_name}" for product in products))
            return 0
        try:
            if args.product:
                product = product_by_name(community, args.product)
            else:
                detection = detect_livery_product(args.install)
                if detection.is_ambiguous:
                    names = ", ".join(detection.candidates)
                    raise InstallError(f"The ZIP contains liveries for multiple aircraft ({names}); use --product.")
                if not detection.product_name:
                    raise InstallError("Could not identify the PMDG aircraft inside this ZIP; use --product.")
                product = product_by_name(community, detection.product_name)
            result = install_livery(args.install, product, args.overwrite)
        except InstallError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
        print(format_result(result))
        return 0
    return -1


def main() -> int:
    parser = argparse.ArgumentParser(description="Install MSFS 2024-native PMDG liveries.")
    parser.add_argument("--detect", action="store_true", help="Print detected Community folders.")
    parser.add_argument("--community", help="MSFS 2024 Community folder.")
    parser.add_argument("--list-products", action="store_true", help="List PMDG aircraft products in Community.")
    parser.add_argument("--product", help="Override detected PMDG product, e.g. pmdg-aircraft-77w.")
    parser.add_argument("--install", metavar="ZIP_OR_FOLDER", help="Install a livery without opening the UI.")
    parser.add_argument("--overwrite", action="store_true", help="Replace matching livery files.")
    args = parser.parse_args()
    cli_result = run_cli(args)
    if cli_result >= 0:
        return cli_result
    launch_gui()
    return 0


def launch_gui() -> None:
    """Load Tk late, allowing all command-line and test use without a display."""

    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD

        root: tk.Tk = TkinterDnD.Tk()
        dnd_enabled = True
    except ImportError:
        root = tk.Tk()
        DND_FILES = None
        dnd_enabled = False

    class InstallerWindow:
        def __init__(self) -> None:
            self.root = root
            self.root.title("PMDG Livery Drop Installer — MSFS 2024")
            self.root.minsize(720, 500)
            self.root.configure(bg="#10202f")
            self.community_var = tk.StringVar(value=str(choose_initial_community() or ""))
            self.product_var = tk.StringVar()
            self.replace_var = tk.BooleanVar(value=False)
            self.status_var = tk.StringVar(value="Drop a livery ZIP to auto-detect its PMDG aircraft.")
            self.products: dict[str, Product] = {}
            self.busy = False
            self.stage_events: queue.Queue[str] = queue.Queue()
            self._build()
            self.refresh_products()

        def _build(self) -> None:
            style = ttk.Style(self.root)
            style.theme_use("clam")
            style.configure("App.TFrame", background="#10202f")
            style.configure("Card.TFrame", background="#17334a")
            style.configure("Heading.TLabel", background="#10202f", foreground="#f5f9fc", font=("Segoe UI", 22, "bold"))
            style.configure("Sub.TLabel", background="#10202f", foreground="#afc5d6", font=("Segoe UI", 10))
            style.configure("Card.TLabel", background="#17334a", foreground="#e7f0f7", font=("Segoe UI", 10))
            style.configure("Status.TLabel", background="#10202f", foreground="#b7dff5", font=("Segoe UI", 10))
            style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 9))

            outer = ttk.Frame(self.root, style="App.TFrame", padding=24)
            outer.pack(fill="both", expand=True)
            ttk.Label(outer, text="PMDG Livery Drop Installer", style="Heading.TLabel").pack(anchor="w")
            ttk.Label(
                outer,
                text="MSFS 2024 • safe ZIP extraction • automatic layout.json rebuild",
                style="Sub.TLabel",
            ).pack(anchor="w", pady=(2, 20))

            settings = ttk.Frame(outer, style="Card.TFrame", padding=16)
            settings.pack(fill="x")
            ttk.Label(settings, text="MSFS 2024 Community folder", style="Card.TLabel").grid(row=0, column=0, sticky="w")
            self.community_entry = ttk.Entry(settings, textvariable=self.community_var, width=76)
            self.community_entry.grid(row=1, column=0, sticky="ew", pady=(5, 10))
            ttk.Button(settings, text="Change…", command=self.pick_community).grid(row=1, column=1, padx=(10, 0), pady=(5, 10))
            ttk.Label(settings, text="PMDG aircraft (auto-detected from ZIP when possible)", style="Card.TLabel").grid(row=2, column=0, sticky="w")
            self.product_combo = ttk.Combobox(settings, textvariable=self.product_var, state="readonly", width=64)
            self.product_combo.grid(row=3, column=0, sticky="ew", pady=(5, 0))
            ttk.Button(settings, text="Rescan", command=self.refresh_products).grid(row=3, column=1, padx=(10, 0), pady=(5, 0))
            settings.columnconfigure(0, weight=1)

            drop = tk.Label(
                outer,
                text="DROP A PMDG MSFS 2024 LIVERY ZIP HERE\n\nThe aircraft and destination package are detected automatically.",
                bg="#0d4d6a",
                fg="#ffffff",
                font=("Segoe UI", 13, "bold"),
                justify="center",
                padx=20,
                pady=35,
                cursor="hand2",
                relief="flat",
            )
            drop.pack(fill="both", expand=True, pady=18)
            drop.bind("<Button-1>", lambda _event: self.browse_livery())
            self.drop_zone = drop
            if dnd_enabled and DND_FILES:
                drop.drop_target_register(DND_FILES)
                drop.dnd_bind("<<Drop>>", self.on_drop)
            else:
                drop.configure(text="SELECT A PMDG MSFS 2024 LIVERY ZIP\n\nDrag-and-drop support needs tkinterdnd2. Click here to browse.")

            actions = ttk.Frame(outer, style="App.TFrame")
            actions.pack(fill="x")
            ttk.Checkbutton(actions, text="Replace matching livery files", variable=self.replace_var).pack(side="left")
            self.progress_bar = ttk.Progressbar(actions, mode="indeterminate", length=230)
            self.progress_bar.pack(side="left", padx=(16, 0))
            ttk.Button(actions, text="Browse for livery ZIP", style="Accent.TButton", command=self.browse_livery).pack(side="right")
            ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel", wraplength=660).pack(anchor="w", pady=(12, 0))

        def pick_community(self) -> None:
            folder = filedialog.askdirectory(title="Select your MSFS 2024 Community folder")
            if folder:
                self.community_var.set(folder)
                self.refresh_products()

        def refresh_products(self) -> None:
            community = self.community_var.get().strip()
            self.products.clear()
            if not community:
                self.product_combo["values"] = []
                self.status_var.set("No Community folder was detected. Select it with Change…")
                return
            products = discover_products(community)
            for product in products:
                self.products[product.display_name] = product
            self.product_combo["values"] = list(self.products)
            if self.product_var.get() not in self.products:
                self.product_var.set(next(iter(self.products), ""))
            save_settings({"community_folder": str(normalized(community))})
            if products:
                self.status_var.set(f"Found {len(products)} PMDG product(s). Drop a ZIP to detect and install it.")
            else:
                self.status_var.set("No PMDG packages were found in this Community folder.")

        def browse_livery(self) -> None:
            livery = filedialog.askopenfilename(
                title="Select an MSFS 2024 PMDG livery ZIP",
                filetypes=[("Livery ZIP", "*.zip"), ("All files", "*.*")],
            )
            if livery:
                self.begin_install(Path(livery))

        def on_drop(self, event: object) -> str:
            raw = getattr(event, "data", "")
            try:
                paths = self.root.tk.splitlist(raw)
            except tk.TclError:
                paths = [raw]
            if len(paths) != 1:
                messagebox.showerror(APP_NAME, "Drop one livery ZIP or extracted folder at a time.")
            elif paths[0]:
                self.begin_install(Path(paths[0]))
            return "break"

        def selected_product(self) -> Product | None:
            return self.products.get(self.product_var.get())

        def product_for_detection(self, source: Path) -> Product | None:
            """Set the combobox only when ZIP internals identify one PMDG product."""

            detection = detect_livery_product(source)
            if detection.is_ambiguous:
                names = ", ".join(name.removeprefix("pmdg-aircraft-").upper() for name in detection.candidates)
                raise InstallError(f"ZIP contains multiple aircraft ({names}); nothing was installed.")
            if not detection.product_name:
                raise InstallError(
                    "Could not verify the PMDG aircraft from this ZIP, so nothing was installed. "
                    "Use a native PMDG MSFS 2024 livery ZIP with livery.json or SimObjects metadata."
                )
            for label, product in self.products.items():
                if product.base_name == detection.product_name:
                    self.product_var.set(label)
                    self.status_var.set(f"Detected {product.display_name} from the ZIP. Installing…")
                    return product
            expected = AIRCRAFT_NAMES[detection.product_name]
            raise InstallError(f"Detected {expected}, but that PMDG product is not installed in the selected Community folder.")

        def begin_install(self, source: Path) -> None:
            if self.busy:
                return
            try:
                product = self.product_for_detection(source)
            except InstallError as error:
                messagebox.showerror(APP_NAME, str(error))
                return
            if not product:
                messagebox.showerror(APP_NAME, "Could not detect an aircraft. Choose the PMDG aircraft in the list, then drop the ZIP again.")
                return
            self.busy = True
            self.drop_zone.configure(bg="#406070", text="INSTALLING LIVERY…")
            self.progress_bar.start(12)
            self.status_var.set(f"Starting installation of {source.name}…")
            self.root.after(75, self.poll_install_stages)
            thread = threading.Thread(target=self._install_worker, args=(source, product, self.replace_var.get()), daemon=True)
            thread.start()

        def poll_install_stages(self) -> None:
            latest_stage = None
            while True:
                try:
                    latest_stage = self.stage_events.get_nowait()
                except queue.Empty:
                    break
            if latest_stage:
                self.status_var.set(latest_stage)
            if self.busy:
                self.root.after(75, self.poll_install_stages)

        def _install_worker(self, source: Path, product: Product, overwrite: bool) -> None:
            try:
                result = install_livery(source, product, overwrite, status_callback=self.stage_events.put)
            except Exception as error:  # UI boundary: surface every safe installer error.
                detail = str(error) or type(error).__name__
                write_diagnostic(f"FAILED [{product.base_name}] {source.name}: {detail}")
                # Do not close over ``error``: Python clears an exception target at
                # the end of an except block, which made packaged failures appear
                # silent when the UI callback ran later.
                self.root.after(0, self.finish_install, None, detail)
            else:
                write_diagnostic(f"INSTALLED [{product.base_name}] {source.name}")
                self.root.after(0, lambda: self.finish_install(result, None))

        def finish_install(self, result: InstallResult | None, error: str | None) -> None:
            self.busy = False
            self.progress_bar.stop()
            self.drop_zone.configure(
                bg="#0d4d6a",
                text="DROP A PMDG MSFS 2024 LIVERY ZIP HERE\n\nThe aircraft and destination package are detected automatically.",
            )
            if error:
                self.status_var.set(f"Install failed: {error}")
                messagebox.showerror("Livery not installed", error)
                return
            assert result is not None
            message = format_result(result)
            self.status_var.set(f"Installed successfully: {result.destinations[0].name}")
            messagebox.showinfo("Livery installed", message)

    InstallerWindow()
    root.mainloop()


if __name__ == "__main__":
    raise SystemExit(main())
