"""Conservative transcript quality annotations; never delete or rewrite source text."""
from __future__ import annotations
import copy
import json
import re
from pathlib import Path


def annotate_utterances(utterances, reviews=None):
    values=copy.deepcopy(utterances)
    normalized=[re.sub(r'[^\w\u4e00-\u9fff]','',u['text']).lower() for u in values]
    suspicious=set()
    for i,text in enumerate(normalized):
        # Long repeated decoder phrases are not reliable summary evidence.
        # Short acknowledgements and ordinary repeated words are deliberately kept.
        if len(text)>=24 and re.search(r'(.{3,40}?)\1{4,}',text):suspicious.add(i)
    start=0
    while start<len(values):
        end=start+1
        while end<len(values) and normalized[end]==normalized[start]:end+=1
        if end-start>=5 and len(normalized[start])>=4:suspicious.update(range(start,end))
        start=end
    for i,u in enumerate(values):
        reasons=[]
        if i in suspicious:reasons.append('连续重复文本，可能为解码异常；原文与原音保留')
        review=(reviews or {}).get(u['id'])
        if review:
            expected=set(review.get('source_sha256',[]))
            actual={s['sha256'] for s in u['source_spans']}
            if expected and expected==actual:reasons.extend(review.get('reasons',[]))
        if reasons:u.update(quality_flags=list(dict.fromkeys(reasons)),exclude_from_summary=True)
    return values


def read_reviews(directory):
    path=Path(directory)/'quality/utterance-review.json'
    if not path.exists():return {}
    value=json.loads(path.read_text())
    if value.get('schema_version')!=1:raise ValueError('unknown utterance review version')
    return value.get('utterances',{})
