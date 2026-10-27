#!/usr/bin/env python3
"""Install the Bosto runtime and optional systemd/udev autostart.

Build as a normal user before installing. --destdir stages files without root,
DKMS commands, systemd activation, udev changes, or reading live processes.
"""
# SPDX-License-Identifier: MIT
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[1]
RUNTIME = Path('/usr/local/lib/bosto-display')
MANIFEST = RUNTIME / 'install-manifest.json'
CONFIG = Path('/etc/default/bosto-display')
UNIT = Path('/etc/systemd/system/bosto-display@.service')
RULE = Path('/etc/udev/rules.d/70-bosto-display.rules')
PEN_QUIRK = Path('/etc/udev/hwdb.d/99-bosto-bt101a.hwdb')
DKMS_NAME = 'bosto-evdi'
DKMS_VERSION = '1.15.1'
DKMS_SOURCE = Path('/usr/src') / f'{DKMS_NAME}-{DKMS_VERSION}'
LICENSE_DIR = Path('/usr/local/share/licenses/bosto-display')


class InstallError(Exception):
    pass


def command(args, *, check=True):
    try:
        result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise InstallError(f'{args[0]}: {exc}') from exc
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise InstallError(f"{' '.join(args)} failed: {detail}")
    return result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def allowed(path):
    return (path in (CONFIG, UNIT, RULE, MANIFEST) or
            path.is_relative_to(RUNTIME) or path.is_relative_to(LICENSE_DIR) or
            path.is_relative_to(DKMS_SOURCE))


def target(root, path):
    if not path.is_absolute() or '..' in path.parts or not allowed(path):
        raise InstallError(f'Unexpected installation path: {path}')
    return root / str(path).lstrip('/')


def old_manifest(root):
    path = target(root, MANIFEST)
    if not path.exists():
        return {}
    if path.is_symlink():
        raise InstallError('Installation manifest must not be a symlink')
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('package') != 'bosto-display':
            raise ValueError('unknown package/schema')
        if not isinstance(data.get('files'), dict):
            raise ValueError('invalid file records')
        for name, sha in data['files'].items():
            target(root, Path(name))
            if not re.fullmatch(r'[0-9a-f]{64}', sha):
                raise ValueError('invalid checksum')
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InstallError(f'Invalid existing installation manifest: {exc}') from exc


def atomic_write(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise InstallError(f'Refusing to replace symlink: {path}')
    fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_manifest(root, data):
    atomic_write(target(root, MANIFEST),
                 (json.dumps(data, indent=2, sort_keys=True) + '\n').encode(), 0o600)


def active_manual_processes():
    """Read only executable identity/cgroup, never input or full command lines."""
    found = []
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            name = os.readlink(process / 'exe').removesuffix(' (deleted)')
            if Path(name).name != 'bosto-display':
                continue
            cgroup = (process / 'cgroup').read_text()
            if re.search(r'/bosto-display@[^/]+\.service(?:/|$)', cgroup):
                continue
            found.append(int(process.name))
        except (OSError, ValueError):
            continue
    return found


def connected_instances():
    instances = []
    for device in Path('/sys/bus/usb/devices').iterdir():
        try:
            if (device / 'idVendor').read_text().strip().lower() != '34c7':
                continue
            if (device / 'idProduct').read_text().strip().lower() != '2700':
                continue
            bus = int((device / 'busnum').read_text())
            number = int((device / 'devnum').read_text())
            instances.append((f'{bus:03d}-{number:03d}', device))
        except (OSError, ValueError):
            continue
    return instances


def dkms_state():
    result = command(['dkms', 'status'], check=False)
    if result.returncode:
        raise InstallError(f'Cannot inspect DKMS: {result.stderr.strip()}')
    names = {line.partition('/')[0].strip() for line in result.stdout.splitlines()}
    foreign = sorted(name for name in names if name != DKMS_NAME and
                     (name == 'evdi' or name.endswith('-evdi')))
    return DKMS_NAME in names, foreign


def file_plan(autostart, dkms):
    plan = {
        RUNTIME / 'bosto-display': (SOURCE / 'build/bosto-display', 0o755),
        RUNTIME / 'libevdi.so.1': (SOURCE / 'build/libevdi.so.1', 0o644),
        RUNTIME / 'evdi.ko': (SOURCE / 'build/evdi.ko', 0o644),
        RUNTIME / 'service-runner.sh': (SOURCE / 'scripts/service-runner.sh', 0o755),
        LICENSE_DIR / 'LICENSE': (SOURCE / 'LICENSE', 0o644),
        LICENSE_DIR / 'EVDI-module-LICENSE': (SOURCE / 'vendor/evdi/module/LICENSE', 0o644),
        LICENSE_DIR / 'EVDI-library-LICENSE': (SOURCE / 'vendor/evdi/library/LICENSE', 0o644),
    }
    if autostart:
        plan.update({UNIT: (SOURCE / 'packaging/bosto-display@.service', 0o644),
                     RULE: (SOURCE / 'packaging/70-bosto-display.rules', 0o644),
                     CONFIG: (SOURCE / 'packaging/bosto-display.conf', 0o644)})
    data = {}
    for destination, (source, mode) in plan.items():
        if not source.is_file():
            raise InstallError(f'Missing {source}; run make and make module as a normal user first')
        data[destination] = (source.read_bytes(), mode)
    for name in ('bosto-display', 'libevdi.so.1', 'evdi.ko'):
        if not data[RUNTIME / name][0].startswith(b'\x7fELF'):
            raise InstallError(f'build/{name} is not an ELF build artifact')
    if dkms:
        for source in (SOURCE / 'vendor/evdi/module').rglob('*'):
            if source.is_symlink():
                raise InstallError(f'DKMS source must not contain symlinks: {source}')
            if not source.is_file():
                continue
            if (source.name.startswith('.') or source.suffix in ('.o', '.ko', '.mod') or
                    source.name in ('Module.symvers', 'modules.order', 'evdi_detect.h') or
                    source.name.endswith('.mod.c')):
                continue
            contents = source.read_bytes()
            if source.name == 'dkms.conf':
                contents = re.sub(rb'^PACKAGE_NAME=.*$', b'PACKAGE_NAME="bosto-evdi"',
                                  contents, flags=re.MULTILINE)
            data[DKMS_SOURCE / source.relative_to(SOURCE / 'vendor/evdi/module')] = (contents, 0o644)
    return data


def check_plan(root, data, previous):
    records = previous.get('files', {})
    for destination, (contents, _) in data.items():
        path = target(root, destination)
        if path.is_symlink():
            raise InstallError(f'Refusing existing symlink: {path}')
        if destination == CONFIG and path.is_file():
            continue  # A local service configuration is always preserved.
        if path.exists():
            current = digest(path)
            new_hash = hashlib.sha256(contents).hexdigest()
            if current not in (new_hash, records.get(str(destination))):
                raise InstallError(f'Refusing to overwrite a local or modified file: {path}')
    # A privileged service must never execute from a user-owned directory.
    if root == Path('/'):
        for directory in (RUNTIME, LICENSE_DIR):
            if directory.exists():
                status = directory.stat()
                if directory.is_symlink() or status.st_uid != 0 or status.st_mode & 0o022:
                    raise InstallError(f'Installation directory is not protected/root-owned: {directory}')


def install(args, root, previous):
    autostart = args.autostart or previous.get('autostart', False)
    want_dkms = args.dkms or previous.get('dkms', {}).get('owned', False)
    registered = False
    if not args.destdir:
        if autostart and not args.no_start:
            manual = active_manual_processes()
            if manual:
                raise InstallError('A manually launched driver is running (PID ' +
                                   ', '.join(map(str, manual)) + '). Stop it with Ctrl+C, then rerun this installer. No files were changed.')
        if want_dkms:
            if not shutil.which('dkms'):
                raise InstallError('DKMS is not installed; install dkms or omit --dkms')
            registered, foreign = dkms_state()
            if foreign:
                print('Preserving existing EVDI DKMS package(s): ' + ', '.join(foreign))
                want_dkms = False
            elif target(root, DKMS_SOURCE).exists() and not previous.get('dkms', {}).get('owned', False):
                raise InstallError(f'{DKMS_SOURCE} already exists outside this installation; refusing to replace it')
    data = file_plan(autostart, want_dkms)
    kernel = os.uname().release
    vermagic = command(['modinfo', '-F', 'vermagic', str(SOURCE / 'build/evdi.ko')]).stdout.strip()
    if not vermagic or vermagic.split()[0] != kernel:
        raise InstallError('The built EVDI module does not match the running kernel; run make module first')
    check_plan(root, data, previous)
    records = dict(previous.get('files', {}))
    for destination, (contents, mode) in data.items():
        path = target(root, destination)
        if destination == CONFIG and path.is_file():
            continue
        atomic_write(path, contents, mode)
        records[str(destination)] = hashlib.sha256(contents).hexdigest()
    result = {'schema_version': 1, 'package': 'bosto-display', 'version': '0.1.0',
              'kernel_release': kernel, 'autostart': autostart, 'files': records,
              'dkms': {'name': DKMS_NAME, 'version': DKMS_VERSION,
                       'requested': want_dkms, 'owned': previous.get('dkms', {}).get('owned', False)}}
    write_manifest(root, result)
    if args.destdir:
        print(f'Staged installation at {root}; no system services, devices, DKMS registrations, or live files were changed.')
        return
    if want_dkms:
        if not registered:
            command(['dkms', 'add', '-m', DKMS_NAME, '-v', DKMS_VERSION])
            result['dkms']['owned'] = True
            write_manifest(root, result)
        command(['dkms', 'build', '-m', DKMS_NAME, '-v', DKMS_VERSION, '-k', kernel])
        command(['dkms', 'install', '-m', DKMS_NAME, '-v', DKMS_VERSION, '-k', kernel])
    if shutil.which('restorecon'):
        command(['restorecon', '-R', str(RUNTIME), str(LICENSE_DIR)], check=False)
    if autostart:
        command(['systemctl', 'daemon-reload'])
        command(['udevadm', 'control', '--reload-rules'])
        if not args.no_start:
            for instance, device in connected_instances():
                command(['udevadm', 'trigger', '--action=add', str(device)])
                command(['udevadm', 'settle', '--timeout=10'])
                # SYSTEMD_WANTS added to an already active device is not enough.
                unit = f'bosto-display@{instance}.service'
                command(['systemctl', 'restart', unit])
                # Type=exec confirms the runner started, not USB/EVDI readiness.
                time.sleep(1)
                if device.exists():
                    command(['systemctl', 'is-active', '--quiet', unit])
        print('Autostart installed: boot/USB add starts the display; unplug stops its device-bound service.')
    else:
        print('Runtime installed. Use --autostart to install boot/USB activation.')
    print('Runtime directory: ' + str(RUNTIME))


def stop_services():
    output = command(['systemctl', 'list-units', '--all', '--plain', '--no-legend',
                      'bosto-display@*.service']).stdout
    units = []
    for line in output.splitlines():
        for word in line.split():
            if re.fullmatch(r'bosto-display@\d{3}-\d{3}\.service', word):
                units.append(word)
                break
    if units:
        command(['systemctl', 'stop', *units])


def uninstall(args, root, previous):
    if not previous:
        raise InstallError('No managed Bosto installation was found')
    if not args.destdir:
        stop_services()
        dkms = previous.get('dkms', {})
        if dkms.get('owned'):
            registered, _ = dkms_state()
            if registered:
                command(['dkms', 'remove', '-m', DKMS_NAME, '-v', DKMS_VERSION, '--all'])
    preserved = []
    for name, checksum in sorted(previous['files'].items(), reverse=True):
        path = target(root, Path(name))
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or digest(path) != checksum:
            preserved.append(str(path))
            continue
        path.unlink()
    if args.remove_pen_quirk:
        path = root / str(PEN_QUIRK).lstrip('/')
        expected = SOURCE / 'udev/99-bosto-bt101a.hwdb'
        if path.is_file() and not path.is_symlink() and path.read_bytes() == expected.read_bytes():
            path.unlink()
            if not args.destdir:
                command(['systemd-hwdb', 'update'])
            print('Removed matching pen quirk; reconnect the tablet for original axis metadata.')
        elif path.exists() or path.is_symlink():
            preserved.append(str(path))
    manifest = target(root, MANIFEST)
    manifest.unlink(missing_ok=True)
    for directory in (DKMS_SOURCE, LICENSE_DIR, RUNTIME):
        path = root / str(directory).lstrip('/')
        if path.exists():
            for child in sorted(path.rglob('*'), reverse=True):
                if child.is_dir() and not child.is_symlink():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            try:
                path.rmdir()
            except OSError:
                pass
    if not args.destdir:
        command(['systemctl', 'daemon-reload'])
        command(['udevadm', 'control', '--reload-rules'])
    print('Managed Bosto runtime/autostart removed. The pen quirk is preserved unless --remove-pen-quirk was requested.')
    if preserved:
        print('Preserved locally modified/unmanaged files:\n  ' + '\n  '.join(preserved))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--autostart', action='store_true', help='install udev/systemd boot and hotplug activation')
    parser.add_argument('--dkms', action='store_true', help='register bundled EVDI for automatic kernel rebuilds; preserve an existing EVDI DKMS package')
    parser.add_argument('--no-start', action='store_true', help='install autostart files without starting an attached display now')
    parser.add_argument('--destdir', type=Path, help='stage files under this directory; never activate host services/DKMS')
    parser.add_argument('--uninstall', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--remove-pen-quirk', action='store_true', help='with uninstall, also remove the exact matching pen hwdb override')
    args = parser.parse_args()
    if args.uninstall and (args.autostart or args.dkms or args.no_start):
        parser.error('install options cannot be combined with --uninstall')
    if args.remove_pen_quirk and not args.uninstall:
        parser.error('--remove-pen-quirk requires scripts/uninstall.sh')
    if args.no_start and not args.autostart:
        parser.error('--no-start requires --autostart')
    root = args.destdir.expanduser().resolve() if args.destdir else Path('/')
    if args.destdir and root == Path('/'):
        parser.error('--destdir must not be /')
    if not args.destdir and os.geteuid() != 0:
        raise InstallError('Run with sudo, or use --destdir to stage without privileges')
    previous = old_manifest(root)
    if args.uninstall:
        uninstall(args, root, previous)
    else:
        install(args, root, previous)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (InstallError, OSError, ValueError, KeyError, TypeError) as error:
        print('Installation: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
