"""Fetch pinned public official source files for an offline API audit."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from urllib.request import urlopen

parser=argparse.ArgumentParser()
parser.add_argument('--output',type=Path,required=True)
args=parser.parse_args()
commits=['781ce14954946366524b96347e7767cefb11a2c8','c94fb25f693900c4ad23b4e4fbe3f7f3f7b57fbc']
jobs=[]
for index,commit in enumerate(commits):
    with urlopen(f'https://api.github.com/repos/zeiss-microscopy/OAD/git/trees/{commit}?recursive=1',timeout=30) as response:
        tree=json.load(response)
    assert not tree.get('truncated'),'Incomplete official tree'
    for item in tree['tree']:
        path=item['path']
        selected=(path.startswith('ZEN-API/protofiles/') and path.endswith('.proto') or path=='ZEN-API/README.md') if index==0 else (path.startswith('ZEN-API/') and (path.endswith('.md') or '/public/' in path and path.endswith('.py')))
        if selected:jobs.append((commit,path))
def fetch(job):
    commit,path=job
    target=args.output/commit/path
    url=f'https://raw.githubusercontent.com/zeiss-microscopy/OAD/{commit}/{path}'
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True)
        with urlopen(url,timeout=60) as response:target.write_bytes(response.read())
    return {'commit':commit,'path':path,'url':url,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
with ThreadPoolExecutor(max_workers=8) as executor:manifest=list(executor.map(fetch,jobs))
args.output.mkdir(parents=True,exist_ok=True)
(args.output/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
print('Fetched/hashed',len(manifest),'official source files')
