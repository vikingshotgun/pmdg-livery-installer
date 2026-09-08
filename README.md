# PMDG Livery Drop Installer — MSFS 2024

A small Windows desktop application for installing **MSFS 2024-native PMDG
livery ZIPs** without having to dig through `Community` folders or run a
separate layout generator.

Drop a compatible livery ZIP onto the window (or choose it with **Browse**) and
the app will inspect it to select the correct installed PMDG aircraft. The
aircraft dropdown displays that detected target; it is not used as an unsafe
fallback. If the ZIP does not identify one aircraft unambiguously, installation
stops before any simulator file is written. It will then:

1. find the MSFS 2024 Community folder;
2. put the livery in the selected aircraft's companion
   `pmdg-aircraft-*-liveries` package;
3. rebuild that package's `layout.json` internally; and
4. retain a dated layout backup in the app-data folder.

It supports the PMDG 737-600/-700/-800/-900 and 777-200ER/-200LR/-300ER/F
naming conventions. Individual livery packages are correctly treated as
sources, never as an aircraft target.

## What it accepts

- MSFS 2024 PMDG livery ZIPs, including a complete `*-liveries` package or a
  ZIP containing `SimObjects`.
- Download wrappers that contain one compatible livery ZIP inside another ZIP
  (up to three nested levels).
- An extracted livery folder containing `livery.cfg` or texture/model/panel
  folders.

`.ptp` files and MSFS 2020-only livery layouts are deliberately not converted.
Export or download an MSFS 2024 ZIP instead. A file cannot be copied safely
when it is unclear which simulator-generation format it targets.

When a wrapper contains more than one plausible livery ZIP, the installer
stops and names the choices rather than guessing which aircraft to install.

## Run from source

Requires Python 3.11 or later on Windows.

```powershell
python -m pip install -r requirements.txt
python .\pmdg_livery_installer.py
```

`tkinterdnd2` enables true Windows drag and drop. If it is unavailable, the
application still works with **Browse for livery ZIP**.

## Build a portable EXE

```powershell
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

The executable is written to `dist\PMDG Livery Drop Installer.exe`.

## How the installer chooses paths

The app reads the MSFS 2024 `UserCfg.opt` files used by Steam and the Microsoft
Store/Xbox version, then looks for `Community` or `Community2024`. You can
always browse to a different Community folder. The installer uses this folder
to find the installed PMDG aircraft.

By default it places the new companion livery package in that same Community
folder. In the desktop app you can instead select **An external folder** and
use its **Browse…** button, which is useful for an Addons Linker library or a
separate add-on drive. The selected external folder receives the
`pmdg-aircraft-*-liveries` package; the base PMDG aircraft is never copied or
modified.

For a selected product such as `pmdg-aircraft-77w`, the normal destination is:

```text
Community\pmdg-aircraft-77w-liveries\SimObjects\Airplanes\PMDG 777-300ER\liveries\pmdg\<livery>
```

If the companion `-liveries` package does not exist yet, the app creates a
minimal Community package and reports its location. It does not alter PMDG's
base aircraft package or PMDG's WASM settings folder.

## Safety behavior

- ZIP paths, ZIP symlinks, and oversized archive metadata are rejected before
  extraction.
- Existing livery files are never replaced unless **Replace matching livery**
  is checked.
- The selected package and its livery destination cannot be symlinks/junctions
  by default. This avoids accidentally editing an Addons Linker source.
- The installer only writes beneath the selected Community folder and backs up
  the old `layout.json` outside the simulator package.
- If an install fails, the window shows the precise reason and the same local
  detail is saved to `%APPDATA%\PMDG Livery Drop Installer\installer.log`.

## Command line

The GUI is the intended workflow, but these commands are handy for validation:

```powershell
python .\pmdg_livery_installer.py --detect
python .\pmdg_livery_installer.py --community "D:\MSFS 2024\Packages\Community" --list-products
python .\pmdg_livery_installer.py --community "D:\MSFS 2024\Packages\Community" --install "D:\Downloads\Livery.zip"
python .\pmdg_livery_installer.py --community "D:\MSFS 2024\Packages\Community" --destination "E:\MSFS Addons\PMDG Liveries" --install "D:\Downloads\Livery.zip"
```

The installer determines the aircraft from the ZIP's internal PMDG package
name, `SimObjects\Airplanes` folder, or compatible configuration references.
When a ZIP contains liveries for multiple aircraft or lacks PMDG metadata, the
installer stops rather than guessing a destination. The UI's animated install
bar and stage text show extraction, copying, and layout rebuild progress.

## Verification

```powershell
python -m unittest discover -s tests -v
```

This project is an independent MSFS 2024 re-engineering of the workflow in
[`dsl94/pmdg-77er-livery-installer`](https://github.com/dsl94/pmdg-77er-livery-installer).
Unlike that MSFS 2020-oriented script, it uses the native MSFS 2024 livery
package hierarchy and does not copy legacy `options.ini` files into PMDG's
WASM work area.
