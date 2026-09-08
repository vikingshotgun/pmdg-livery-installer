import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path

import pmdg_livery_installer as installer


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.community = self.root / "Packages" / "Community"
        self.community.mkdir(parents=True)
        self.product_root = self.community / "pmdg-aircraft-77w"
        (self.product_root / "SimObjects" / "Airplanes" / "PMDG 777-300ER").mkdir(parents=True)
        (self.product_root / "layout.json").write_text('{"content": []}\n', encoding="utf-8")
        self.old_backup_root = installer.BACKUP_ROOT
        installer.BACKUP_ROOT = self.root / "backups"

    def tearDown(self):
        installer.BACKUP_ROOT = self.old_backup_root
        self.tmp.cleanup()

    def product(self):
        products = installer.discover_products(self.community)
        self.assertEqual([item.base_name for item in products], ["pmdg-aircraft-77w"])
        return products[0]

    def build_zip(self, name, files):
        archive = self.root / name
        with zipfile.ZipFile(archive, "w") as zipped:
            for filename, content in files.items():
                zipped.writestr(filename, content)
        return archive

    @staticmethod
    def zip_bytes(files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zipped:
            for filename, content in files.items():
                zipped.writestr(filename, content)
        return buffer.getvalue()

    def test_installs_simobjects_zip_in_companion_livery_package(self):
        archive = self.build_zip(
            "airline.zip",
            {
                "Download/SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Test Airline/livery.cfg": "title=Test Airline\n",
                "Download/SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Test Airline/texture.test/fuselage.png": "paint",
            },
        )

        stages = []
        result = installer.install_livery(archive, self.product(), status_callback=stages.append)
        installed = (
            self.community
            / "pmdg-aircraft-77w-liveries"
            / "SimObjects"
            / "Airplanes"
            / "PMDG 777-300ER"
            / "liveries"
            / "pmdg"
            / "Test Airline"
        )
        self.assertEqual(result.livery_package, self.community / "pmdg-aircraft-77w-liveries")
        self.assertTrue((installed / "livery.cfg").is_file())
        self.assertTrue((installed / "texture.test" / "fuselage.png").is_file())
        self.assertFalse((self.product_root / "liveries").exists())

        layout = json.loads((result.livery_package / "layout.json").read_text(encoding="utf-8"))
        paths = {entry["path"] for entry in layout["content"]}
        self.assertIn(
            "SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Test Airline/livery.cfg",
            paths,
        )
        manifest = json.loads((result.livery_package / "manifest.json").read_text(encoding="utf-8"))
        self.assertGreater(int(manifest["total_package_size"]), 0)
        self.assertEqual(
            stages,
            [
                "Extracting and inspecting the livery ZIP…",
                "Preparing the PMDG 777-300ER  [77w] livery package…",
                "Copying livery files…",
                "Rebuilding layout.json for MSFS 2024…",
                "Finalizing installation…",
            ],
        )

    def test_layout_dates_use_unix_epoch_ticks_like_pmdg_packages(self):
        package = self.community / "pmdg-aircraft-77w-liveries"
        package.mkdir()
        asset = package / "SimObjects" / "Airplanes" / "PMDG 777-300ER" / "liveries" / "pmdg" / "Test" / "livery.cfg"
        asset.parent.mkdir(parents=True)
        asset.write_text("title=Test\n", encoding="utf-8")
        unix_timestamp = 1_786_253_513.0829274
        os.utime(asset, (unix_timestamp, unix_timestamp))

        installer.rebuild_layout(package, self.product())

        layout = json.loads((package / "layout.json").read_text(encoding="utf-8"))
        entry = next(item for item in layout["content"] if item["path"].endswith("livery.cfg"))
        self.assertEqual(entry["date"], asset.stat().st_mtime_ns // 100)

    def test_installs_livery_zip_wrapped_inside_another_zip(self):
        inner_zip = self.zip_bytes(
            {
                "SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Wrapped/livery.cfg": "title=Wrapped\n",
                "SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Wrapped/texture/main.png": "paint",
            }
        )
        outer_zip = self.build_zip(
            "download-wrapper.zip",
            {"Read me.txt": "Open the nested archive", "PMDG native livery.zip": inner_zip},
        )

        self.assertEqual(installer.detect_livery_product(outer_zip).product_name, "pmdg-aircraft-77w")
        result = installer.install_livery(outer_zip, self.product())
        installed = result.livery_package / "SimObjects" / "Airplanes" / "PMDG 777-300ER" / "liveries" / "pmdg" / "Wrapped"
        self.assertTrue((installed / "livery.cfg").is_file())
        self.assertTrue((installed / "texture" / "main.png").is_file())

    def test_refuses_ambiguous_nested_livery_archives(self):
        first = self.zip_bytes({"SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/One/livery.cfg": "title=One"})
        second = self.zip_bytes({"SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Two/livery.cfg": "title=Two"})
        outer = self.build_zip("two-liveries.zip", {"first.zip": first, "second.zip": second})
        with self.assertRaisesRegex(installer.InstallError, "multiple possible livery archives"):
            installer.install_livery(outer, self.product())

    def test_detects_aircraft_from_native_airplane_folder_inside_zip(self):
        archive = self.build_zip(
            "unhelpful-download-name.zip",
            {"Wrapped/SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Test/livery.cfg": "title=Test\n"},
        )
        detected = installer.detect_livery_product(archive)
        self.assertEqual(detected.product_name, "pmdg-aircraft-77w")
        self.assertIn("airplane folder: PMDG 777-300ER", detected.evidence)

    def test_detects_aircraft_from_pmdg_package_name_inside_zip(self):
        archive = self.build_zip(
            "airline.zip",
            {"pmdg-aircraft-738-airline/SimObjects/Airplanes/Anything/livery.cfg": "title=Test\n"},
        )
        detected = installer.detect_livery_product(archive)
        self.assertEqual(detected.product_name, "pmdg-aircraft-738")

    def test_detects_aircraft_from_pmdg_livery_json_metadata(self):
        archive = self.build_zip(
            "generic-name.zip",
            {
                "livery/livery.cfg": "title=AeroLogic\n",
                "livery/livery.json": '{"productPackage": "pmdg-aircraft-77f"}',
            },
        )
        detected = installer.detect_livery_product(archive)
        self.assertEqual(detected.product_name, "pmdg-aircraft-77f")

    def test_reports_multiple_detected_aircraft_as_ambiguous(self):
        archive = self.build_zip(
            "fleet.zip",
            {
                "SimObjects/Airplanes/PMDG 737-800/liveries/pmdg/One/livery.cfg": "title=One\n",
                "SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Two/livery.cfg": "title=Two\n",
            },
        )
        detected = installer.detect_livery_product(archive)
        self.assertTrue(detected.is_ambiguous)
        self.assertIsNone(detected.product_name)
        self.assertEqual(set(detected.candidates), {"pmdg-aircraft-738", "pmdg-aircraft-77w"})

    def test_installs_direct_livery_in_detected_aircraft_folder(self):
        source = self.root / "Direct Livery"
        (source / "texture.example").mkdir(parents=True)
        (source / "livery.cfg").write_text("title=Direct\n", encoding="utf-8")
        (source / "livery.json").write_text('{"productPackage": "pmdg-aircraft-77w"}\n', encoding="utf-8")
        (source / "texture.example" / "main.png").write_bytes(b"paint")

        result = installer.install_livery(source, self.product())
        installed = result.livery_package / "SimObjects" / "Airplanes" / "PMDG 777-300ER" / "liveries" / "pmdg" / "Direct Livery"
        self.assertTrue((installed / "texture.example" / "main.png").is_file())

    def test_refuses_second_install_unless_overwrite_is_requested(self):
        source = self.root / "Direct Livery"
        (source / "texture.example").mkdir(parents=True)
        (source / "livery.cfg").write_text("title=Direct\n", encoding="utf-8")
        (source / "livery.json").write_text('{"productPackage": "pmdg-aircraft-77w"}\n', encoding="utf-8")
        installer.install_livery(source, self.product())
        with self.assertRaisesRegex(installer.InstallError, "matching livery"):
            installer.install_livery(source, self.product())
        result = installer.install_livery(source, self.product(), overwrite=True)
        self.assertGreaterEqual(result.copied_files, 1)

    def test_blocks_install_when_detected_aircraft_does_not_match_target(self):
        source = self.root / "777F Livery"
        (source / "texture.example").mkdir(parents=True)
        (source / "livery.cfg").write_text("title=777F\n", encoding="utf-8")
        (source / "livery.json").write_text('{"productPackage": "pmdg-aircraft-77f"}\n', encoding="utf-8")
        with self.assertRaisesRegex(installer.InstallError, "identifies itself as PMDG 777F"):
            installer.install_livery(source, self.product())
        self.assertFalse((self.community / "pmdg-aircraft-77w-liveries").exists())

    def test_refuses_archive_path_traversal(self):
        archive = self.build_zip("unsafe.zip", {"../outside.txt": "nope"})
        with self.assertRaisesRegex(installer.InstallError, "Unsafe path"):
            installer.install_livery(archive, self.product())
        self.assertFalse((self.root / "outside.txt").exists())

    def test_refuses_duplicate_zip_file_names(self):
        archive = self.root / "duplicate.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("SimObjects/Airplanes/PMDG 777-300ER/liveries/pmdg/Test/livery.cfg", "first")
            zipped.writestr("simobjects/airplanes/pmdg 777-300er/liveries/pmdg/test/LIVERY.CFG", "second")
        with self.assertRaisesRegex(installer.InstallError, "more than once"):
            installer.install_livery(archive, self.product())

    def test_refuses_full_package_for_a_different_aircraft(self):
        archive = self.build_zip(
            "wrong.zip",
            {"pmdg-aircraft-738-liveries/SimObjects/Airplanes/PMDG 737-800/liveries/pmdg/Test/livery.cfg": "x"},
        )
        with self.assertRaisesRegex(installer.InstallError, "identifies itself as PMDG 737-800"):
            installer.install_livery(archive, self.product())

    def test_parses_usercfg_installed_packages_path(self):
        cfg = self.root / "UserCfg.opt"
        expected = self.root / "A folder" / "Packages"
        cfg.write_text(f'foo=bar\nInstalledPackagesPath "{expected}"\n', encoding="utf-8")
        self.assertEqual(installer.parse_installed_packages_path(cfg), expected.absolute())

    def test_existing_companion_package_is_discovered_as_a_product(self):
        shutil_target = self.community / "pmdg-aircraft-738-liveries"
        shutil_target.mkdir()
        names = [product.base_name for product in installer.discover_products(self.community)]
        self.assertEqual(names, ["pmdg-aircraft-738", "pmdg-aircraft-77w"])


if __name__ == "__main__":
    unittest.main()
