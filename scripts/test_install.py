#!/usr/bin/env python3
"""Offline installation tests. No root, devices, real DKMS, or systemd calls."""
# SPDX-License-Identifier: MIT
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('bosto_install', Path(__file__).with_name('install.py'))
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)
PROJECT = Path(__file__).resolve().parents[1]


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.stage = self.root / 'stage'
        for directory in ('packaging', 'scripts', 'udev'):
            shutil.copytree(PROJECT / directory, self.source / directory)
        (self.source / 'build').mkdir()
        for name in ('bosto-display', 'libevdi.so.1', 'evdi.ko'):
            (self.source / 'build' / name).write_bytes(b'\x7fELFoffline-test-fixture')
        (self.source / 'LICENSE').write_text('MIT test fixture\n')
        for directory in ('module', 'library'):
            path = self.source / 'vendor/evdi' / directory
            path.mkdir(parents=True)
            (path / 'LICENSE').write_text('upstream license fixture\n')
        (self.source / 'vendor/evdi/module/dkms.conf').write_text('PACKAGE_NAME="evdi"\nAUTOINSTALL=yes\n')
        self.source_patch = patch.object(INSTALL, 'SOURCE', self.source)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.external = []
        def command(args, **kwargs):
            self.external.append(args)
            if args[:3] != ['modinfo', '-F', 'vermagic']:
                raise AssertionError('A staging test attempted a live command: ' + repr(args))
            return SimpleNamespace(stdout=os.uname().release + ' SMP\n', stderr='', returncode=0)
        self.command_patch = patch.object(INSTALL, 'command', command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def arguments(self, **overrides):
        values = dict(autostart=True, dkms=True, no_start=False,
                      destdir=self.stage, remove_pen_quirk=False)
        values.update(overrides)
        return SimpleNamespace(**values)

    def install(self, **overrides):
        with contextlib.redirect_stdout(io.StringIO()):
            INSTALL.install(self.arguments(**overrides), self.stage,
                            INSTALL.old_manifest(self.stage))
        return INSTALL.old_manifest(self.stage)

    def test_staging_installs_root_layout_and_never_activates_host(self):
        manifest = self.install()
        runtime = INSTALL.target(self.stage, INSTALL.RUNTIME / 'bosto-display')
        self.assertTrue(runtime.is_file())
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o755)
        self.assertTrue(INSTALL.target(self.stage, INSTALL.UNIT).exists())
        self.assertTrue(INSTALL.target(self.stage, INSTALL.RULE).exists())
        conf = INSTALL.target(self.stage, INSTALL.DKMS_SOURCE / 'dkms.conf').read_text()
        self.assertIn('PACKAGE_NAME="bosto-evdi"', conf)
        self.assertFalse(manifest['dkms']['owned'])
        self.assertTrue(manifest['dkms']['requested'])
        self.assertEqual(len(self.external), 1)

    def test_reinstall_preserves_config_and_rejects_modified_program(self):
        self.install()
        config = INSTALL.target(self.stage, INSTALL.CONFIG)
        config.write_text('BOSTO_FPS=30\n')
        self.install()
        self.assertEqual(config.read_text(), 'BOSTO_FPS=30\n')
        program = INSTALL.target(self.stage, INSTALL.RUNTIME / 'bosto-display')
        program.write_bytes(b'local program override')
        with self.assertRaisesRegex(INSTALL.InstallError, 'local or modified'):
            self.install()
        self.assertEqual(program.read_bytes(), b'local program override')

    def test_uninstall_preserves_local_config_and_pen_quirk(self):
        manifest = self.install()
        config = INSTALL.target(self.stage, INSTALL.CONFIG)
        config.write_text('BOSTO_FPS=20\n')
        quirk = self.stage / str(INSTALL.PEN_QUIRK).lstrip('/')
        quirk.parent.mkdir(parents=True)
        quirk.write_bytes((self.source / 'udev/99-bosto-bt101a.hwdb').read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            INSTALL.uninstall(self.arguments(), self.stage, manifest)
        self.assertFalse(INSTALL.target(self.stage, INSTALL.RUNTIME / 'bosto-display').exists())
        self.assertFalse(INSTALL.target(self.stage, INSTALL.UNIT).exists())
        self.assertEqual(config.read_text(), 'BOSTO_FPS=20\n')
        self.assertTrue(quirk.exists())
        self.assertEqual(len(self.external), 1)

    def test_explicit_quirk_removal_only_removes_exact_known_bytes(self):
        manifest = self.install()
        quirk = self.stage / str(INSTALL.PEN_QUIRK).lstrip('/')
        quirk.parent.mkdir(parents=True)
        quirk.write_bytes((self.source / 'udev/99-bosto-bt101a.hwdb').read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            INSTALL.uninstall(self.arguments(remove_pen_quirk=True), self.stage, manifest)
        self.assertFalse(quirk.exists())

    def test_manual_driver_refusal_precedes_all_file_changes(self):
        with patch.object(INSTALL, 'active_manual_processes', return_value=[12345]), \
             patch.object(INSTALL, 'atomic_write') as write:
            with self.assertRaisesRegex(INSTALL.InstallError, '12345'):
                INSTALL.install(self.arguments(destdir=None), self.stage, {})
            write.assert_not_called()
        self.assertFalse(self.stage.exists())

    def test_manifest_cannot_direct_uninstall_outside_owned_paths(self):
        manifest = INSTALL.target(self.stage, INSTALL.MANIFEST)
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({'schema_version':1, 'package':'bosto-display',
                                       'files':{'/etc/passwd':'a' * 64}}))
        with self.assertRaisesRegex(INSTALL.InstallError, 'Unexpected installation path'):
            INSTALL.old_manifest(self.stage)

    def test_dkms_ownership_is_recorded_and_only_owned_package_removed(self):
        calls = []
        def fake_command(args, **kwargs):
            calls.append(args)
            output = os.uname().release + ' SMP\n' if args[0] == 'modinfo' else ''
            return SimpleNamespace(stdout=output, stderr='', returncode=0)
        with patch.object(INSTALL, 'command', fake_command), \
             patch.object(INSTALL, 'active_manual_processes', return_value=[]), \
             patch.object(INSTALL, 'dkms_state', side_effect=[(False, []), (True, [])]), \
             patch.object(INSTALL.shutil, 'which', side_effect=lambda tool: 'dkms' if tool == 'dkms' else None), \
             contextlib.redirect_stdout(io.StringIO()):
            INSTALL.install(self.arguments(autostart=False, destdir=None), self.stage, {})
            manifest = INSTALL.old_manifest(self.stage)
            self.assertTrue(manifest['dkms']['owned'])
            self.assertIn(['dkms', 'add', '-m', 'bosto-evdi', '-v', '1.15.1'], calls)
            INSTALL.uninstall(self.arguments(destdir=None), self.stage, manifest)
        self.assertIn(['dkms', 'remove', '-m', 'bosto-evdi', '-v', '1.15.1', '--all'], calls)
        self.assertFalse(any('evdi' in args[1:] for args in calls if args[0] == 'dkms'))

    def test_foreign_evdi_dkms_is_never_replaced(self):
        calls = []
        def fake_command(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(stdout=os.uname().release + ' SMP\n', stderr='', returncode=0)
        with patch.object(INSTALL, 'command', fake_command), \
             patch.object(INSTALL, 'active_manual_processes', return_value=[]), \
             patch.object(INSTALL, 'dkms_state', return_value=(False, ['evdi'])), \
             patch.object(INSTALL.shutil, 'which', side_effect=lambda tool: 'dkms' if tool == 'dkms' else None), \
             contextlib.redirect_stdout(io.StringIO()):
            INSTALL.install(self.arguments(autostart=False, destdir=None), self.stage, {})
        manifest = INSTALL.old_manifest(self.stage)
        self.assertFalse(manifest['dkms']['owned'])
        self.assertFalse(INSTALL.target(self.stage, INSTALL.DKMS_SOURCE).exists())
        self.assertFalse(any(args[0] == 'dkms' for args in calls))


if __name__ == '__main__':
    unittest.main()
