"""Bounded A52s model verification; restores connectivity in finally."""
from pathlib import Path
import datetime
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import time

ROOT = Path(__file__).resolve().parent
ADB = ['C:/Users/pyomo/AppData/Local/Android/Sdk/platform-tools/adb.exe', '-s', 'R5CRC0XPS2K']

def command(*args, timeout=60, check=True):
    return subprocess.run(ADB + list(args), capture_output=True, timeout=timeout, check=check)

def string(*args, **kwargs):
    return command(*args, **kwargs).stdout.decode('utf-8', errors='replace').strip()

def digest(blob):
    return hashlib.sha256(blob).hexdigest()

def old_app_files():
    blob = command('exec-out', 'run-as', 'app.sleepwell', 'tar', '-cf', '-', 'files', 'shared_prefs').stdout
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        for entry in archive.getmembers():
            if entry.isfile():
                entries[entry.name] = {'bytes': entry.size, 'sha256': digest(archive.extractfile(entry).read())}
    return entries

def main():
    test_apk = Path(sys.argv[1])
    assert digest(test_apk.read_bytes()) == sys.argv[2], 'Wrong test APK'
    installed = command('install', '-r', str(test_apk), timeout=120)
    assert b'Success' in installed.stdout, installed.stdout.decode(errors='replace')
    wifi = string('shell', 'settings', 'get', 'global', 'wifi_on')
    mobile = string('shell', 'settings', 'get', 'global', 'mobile_data')
    assert wifi in ('0', '1') and mobile in ('0', '1'), 'Unexpected initial connectivity state'
    report = {'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'device': 'SM-A528N', 'package': 'app.sleepwell.offlinehgb',
              'test_apk_sha256': sys.argv[2], 'initial_connectivity': {'wifi': wifi, 'mobile_data': mobile}}
    try:
        command('shell', 'svc', 'wifi', 'disable')
        command('shell', 'svc', 'data', 'disable')
        time.sleep(3)
        assert string('shell', 'settings', 'get', 'global', 'wifi_on') == '0'
        assert string('shell', 'settings', 'get', 'global', 'mobile_data') == '0'
        connectivity = string('shell', 'dumpsys', 'connectivity')
        (ROOT / 'private/connectivity_during_test.txt').write_text(connectivity, encoding='utf-8')
        active = re.search(r'Active default network:\s*([^\r\n]+)', connectivity)
        assert active and active.group(1).strip().lower() in ('none', 'null'), 'Active network still present'
        report['active_default_network_during_test'] = active.group(1).strip()
        output = string('shell', 'am', 'instrument', '-w', '-r', '-e', 'class',
                        'app.sleepwell.user.LocalHgbIntegrationTest',
                        'app.sleepwell.offlinehgb.test/android.test.InstrumentationTestRunner', timeout=240)
        (ROOT / 'a52s_offline_instrumentation.txt').write_text(output, encoding='utf-8')
        assert 'OK (6 tests)' in output and 'FAILURES!!!' not in output, output
        match = re.search(r'offline_hgb_audit=(\{[^\r\n]+\})', output)
        assert match, 'Missing production-wiring audit'
        report['production_wiring'] = json.loads(match.group(1))
        report['instrumentation'] = {'tests': 6, 'passed': 6, 'internet_disconnected': True}
        assert report['production_wiring']['history_count'] == 28
    finally:
        command('shell', 'svc', 'wifi', 'enable' if wifi == '1' else 'disable', check=False)
        command('shell', 'svc', 'data', 'enable' if mobile == '1' else 'disable', check=False)
        time.sleep(2)
        report['restored_connectivity'] = {
            'wifi': string('shell', 'settings', 'get', 'global', 'wifi_on'),
            'mobile_data': string('shell', 'settings', 'get', 'global', 'mobile_data')}
        (ROOT / 'a52s_install_verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    before = json.loads((ROOT / 'private/old_app_before.json').read_text(encoding='utf-8'))['files']
    after = old_app_files()
    assert before == after, 'Old app stored files changed'
    assert report['initial_connectivity'] == report['restored_connectivity']
    report['old_app_stored_files_unchanged'] = True
    report['old_app_file_count'] = len(after)
    report['status'] = 'pass'
    (ROOT / 'a52s_install_verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
