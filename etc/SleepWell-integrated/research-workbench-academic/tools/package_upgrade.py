"""Package source + explicitly synthetic examples; exclude user projects and models."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT=Path(__file__).resolve().parents[1]
DIRECTORIES=('researchbench','vendor','samples','tests','tools','scripts','docs','evaluation','examples',
             'deliverables/upgrade_examples','deliverables/upgrade_validation')

def build():
    files=set()
    for directory in DIRECTORIES:
        files.update(p for p in (ROOT/directory).rglob('*') if p.is_file())
    files.update(p for p in ROOT.iterdir() if p.is_file() and p.suffix.lower() in {'.md','.txt','.cmd','.ps1','.json'})
    files=sorted(p for p in files if '__pycache__' not in p.parts and p.suffix not in {'.pyc','.tmp','.lnk'} and not p.name.endswith(('-wal','-shm')))
    # Retain failed-test logs but distribute only the final reviewed PDF renders.
    validation=ROOT/'deliverables'/'upgrade_validation'
    files=[p for p in files if not (p.is_relative_to(validation) and p.suffix.lower()=='.png'
                                   and 'final_render' not in p.relative_to(validation).parts)]
    target=ROOT/'release'/'SleepWell_Research_RAG_20261101_source.zip'
    target.parent.mkdir(exist_ok=True)
    manifest={'schema':1,'package':'SleepWell Research RAG','app_version':'0.2.0','synthetic_examples_only':True,'files':[]}
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in files:
            rel=path.relative_to(ROOT).as_posix(); data=path.read_bytes()
            archive.writestr('SleepWell_Research_RAG/'+rel,data)
            manifest['files'].append({'path':rel,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        archive.writestr('SleepWell_Research_RAG/PACKAGE_MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(digest+'  '+target.name+'\n',encoding='ascii')
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:raise RuntimeError('archive_integrity_failed')
        assert not any('/.venv/' in n or '/.models/' in n or '/projects/' in n for n in archive.namelist())
    print(json.dumps({'archive':str(target),'sha256':digest,'files':len(files),'bytes':target.stat().st_size},ensure_ascii=False))
    return target

if __name__=='__main__':build()
