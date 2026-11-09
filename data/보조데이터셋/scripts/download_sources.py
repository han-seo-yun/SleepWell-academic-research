"""Download public CDC/NHANES data and matching documentation, retaining provenance."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
FILES = [(2017,"DEMO_J","xpt"),(2017,"PAQ_J","xpt"),(2017,"SLQ_J","xpt"),
         (2013,"DEMO_H","xpt"),(2013,"PAXDAY_H","xpt"),(2013,"PAXHR_H","xpt"),
         (2013,"PAXHD_H","xpt"),(2005,"DEMO_D","xpt"),(2005,"PAXRAW_D","zip")]


def download(year, name, extension):
    base=f"https://wwwn.cdc.gov/Nchs/Data/Nhanes/Public/{year}/DataFiles/{name}"
    folder=ROOT/"raw"/f"nhanes_{year}_{year+1}"
    folder.mkdir(parents=True,exist_ok=True)
    docs=ROOT/"sources";docs.mkdir(exist_ok=True)
    target=folder/f"{name}.{extension}"
    url=base+"."+extension
    if not target.exists():
        partial=target.with_suffix(target.suffix+".part")
        with requests.get(url,stream=True,timeout=(30,180)) as response:
            response.raise_for_status()
            if "text/html" in response.headers.get("Content-Type",""):
                raise ValueError(f"Expected binary dataset, received HTML: {url}")
            with partial.open("wb") as out:
                for block in response.iter_content(1024*1024):out.write(block)
        partial.rename(target)
    digest=hashlib.sha256()
    with target.open("rb") as source:
        prefix=source.read(80);digest.update(prefix)
        expected=b"PK" if extension=="zip" else b"HEADER RECORD"
        if not prefix.startswith(expected):raise ValueError(f"Invalid signature: {target}")
        for block in iter(lambda:source.read(1024*1024),b""):digest.update(block)
    doc=requests.get(base+".htm",timeout=60);doc.raise_for_status()
    (docs/f"{name}.html").write_bytes(doc.content)
    from bs4 import BeautifulSoup
    (docs/f"{name}.txt").write_text(BeautifulSoup(doc.text,"html.parser").get_text("\n",strip=True),encoding="utf-8")
    record={"dataset":name,"cycle":f"{year}-{year+1}","url":url,"documentation_url":base+".htm",
            "local_path":str(target.relative_to(ROOT)),"bytes":target.stat().st_size,"sha256":digest.hexdigest(),
            "retrieved_at":datetime.now(timezone.utc).isoformat(),"provider":"CDC/NCHS NHANES",
            "use_conditions":"Public-use statistical analysis only; no re-identification or linkage to identifiable individuals",
            "terms_url":"https://www.cdc.gov/nchs/policy/data-user-agreement.html"}
    print(f"Downloaded {name}: {record['bytes']:,} bytes",flush=True)
    return record


def main():
    results=[];errors=[]
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs={pool.submit(download,*entry):entry for entry in FILES}
        for job in as_completed(jobs):
            try:results.append(job.result())
            except Exception as e:errors.append({"file":jobs[job],"error":str(e)});print(errors[-1],flush=True)
            (ROOT/"manifest.json").write_text(json.dumps({"files":sorted(results,key=lambda x:x['dataset']),"errors":errors},ensure_ascii=False,indent=2),encoding="utf-8")
    for name,url in [("NCHS_DATA_USE","https://www.cdc.gov/nchs/policy/data-user-agreement.html"),
                     ("NHANES_WEIGHTING","https://wwwn.cdc.gov/nchs/nhanes/tutorials/weighting.aspx")]:
        response=requests.get(url,timeout=60);response.raise_for_status()
        (ROOT/"sources"/f"{name}.html").write_bytes(response.content)
    if errors:raise SystemExit(1)


if __name__=="__main__":main()
