from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET
import json

sys.stdout.reconfigure(encoding='utf-8')
root=Path(__file__).resolve().parent
name=sys.argv[1]
assert name.replace('_','').isalnum()
adb=['C:/Users/pyomo/AppData/Local/Android/Sdk/platform-tools/adb.exe','-s','R5CRC0XPS2K']
def run(args):
    return subprocess.run(adb+args,capture_output=True,check=True).stdout
run(['shell','uiautomator','dump','/sdcard/sleepwell_health_check.xml'])
data=run(['shell','cat','/sdcard/sleepwell_health_check.xml'])
(root/(name+'.xml')).write_bytes(data)
(root/(name+'.png')).write_bytes(run(['exec-out','screencap','-p']))
for n in ET.fromstring(data).iter('node'):
    if n.get('text') or n.get('content-desc'):
        print(json.dumps({k:n.get(k) for k in ('text','content-desc','resource-id','bounds','clickable','checked')},ensure_ascii=False))
