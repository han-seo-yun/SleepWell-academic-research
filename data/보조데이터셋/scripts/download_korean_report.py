"""Save the official Korean report unchanged, with its restrictive reuse terms."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import re
import requests
from bs4 import BeautifulSoup

ROOT=Path(__file__).resolve().parents[1]
PAGE='https://www.mcst.go.kr/site/s_policy/dept/deptView.jsp?pDataCD=0417000000&pSeq=1967&pType='
page=requests.get(PAGE,timeout=60);page.raise_for_status()
soup=BeautifulSoup(page.text,'html.parser')
link=next(a for a in soup.find_all('a',onclick=True) if 'file_download(' in a['onclick'])
args=re.findall(r"'([^']*)'",link['onclick'])
url='https://www.mcst.go.kr/servlets/eduport/front/upload/UplDownloadFile?pFileName='+args[0]+'&pRealName='+args[1]+'&pPath='+args[2]+'&pFlag='
response=requests.get(url,timeout=120);response.raise_for_status()
if not response.content.startswith(b'%PDF'):raise ValueError('Expected PDF')
folder=ROOT/'raw/korea';folder.mkdir(parents=True,exist_ok=True)
target=folder/'2024_국민생활체육조사.pdf';target.write_bytes(response.content)
(ROOT/'sources/MCST_2024.html').write_bytes(page.content)
manifest={'title':'2024년 국민생활체육조사','publisher':'문화체육관광부','page_url':PAGE,'download_url':url,
    'local_path':str(target.relative_to(ROOT)),'bytes':len(response.content),'sha256':hashlib.sha256(response.content).hexdigest(),
    'retrieved_at':datetime.now(timezone.utc).isoformat(),'license':'공공누리 제4유형: 출처표시, 상업적 이용금지, 변경금지',
    'role':'Original report for reference only; not participant-level raw data; not used to train or automatically populate app priors'}
(ROOT/'korea_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(manifest,ensure_ascii=False,indent=2))
