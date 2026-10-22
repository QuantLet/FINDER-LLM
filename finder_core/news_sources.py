from __future__ import annotations
import calendar
from datetime import date, timedelta
from functools import lru_cache
import re
from collections import Counter
from datetime import datetime
import math

MONTHS = {name.lower(): i for i in range(1, 13) for name in (calendar.month_name[i], calendar.month_abbr[i])}
MONTH = '(?:' + '|'.join(sorted(MONTHS, key=len, reverse=True)) + ')'
DATED = re.compile(r'\b(?:(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month>' + MONTH + r')|(?P<month2>' + MONTH + r')\s+(?P<day2>\d{1,2})(?:st|nd|rd|th)?)(?:,?\s+(?P<year>(?:19|20)\d{2}))?\b', re.I)
PROSPECTIVE = re.compile(r'\b(?:will|scheduled|due|set to|plans? to|expected to|deadline|next week|tomorrow|ongoing|continues?|until)\b', re.I)
MONTH_RANGE = re.compile(r'\b(?P<part>early|mid|middle of|late)[\s-]+(?P<month>' + MONTH + r')(?:\s+(?P<year>(?:19|20)\d{2}))?\b', re.I)


def catalogue_date_preview(article, timing):

    body = str(article.get('content', article.get('body', '')))
    publication = date.fromisoformat(article['published_at'][:10])
    target, horizon = date.fromisoformat(timing['forecast_target_date']), date.fromisoformat(timing['future_horizon_end'])
    matches = []
    for match in DATED.finditer(body):
        try:
            day = date(int(match['year'] or publication.year), MONTHS[(match['month'] or match['month2']).lower()], int(match['day'] or match['day2']))
        except ValueError:
            continue
        if target <= day <= horizon:matches.append((match.start(), match.end()))
    for match in MONTH_RANGE.finditer(body):
        month, year = MONTHS[match['month'].lower()], int(match['year'] or publication.year)
        part = match['part'].lower()
        start, end = (1,10) if part=='early' else (21,calendar.monthrange(year,month)[1]) if part=='late' else (11,20)
        if date(year,month,start) <= horizon and date(year,month,end) >= target:matches.append((match.start(),match.end()))
    if not matches:return ''
    start, end = min(matches)
    return body[max(0,start-90):min(len(body),end+110)]


@lru_cache(maxsize=256)
def earliest_retained_date(calendar_id, target):
    from .future_events import retention_end
    day = date.fromisoformat(target)
    while retention_end(calendar_id, (day-timedelta(days=1)).isoformat()) >= target:
        day -= timedelta(days=1)
    return day


def candidate_previews(article, timing):
    body = str(article.get('content', article.get('body', '')))
    target = date.fromisoformat(timing['forecast_target_date'])
    publication = date.fromisoformat(article.get('published_at', article.get('webPublicationDate'))[:10])
    end = date.fromisoformat(timing['future_horizon_end'])
    earliest = earliest_retained_date(timing["future_calendar_id"], target.isoformat())
    recent = publication >= earliest
    matches = []
    for match in DATED.finditer(body):
        try:
            day = date(int(match['year'] or publication.year), MONTHS[(match['month'] or match['month2']).lower()], int(match['day'] or match['day2']))
        except ValueError:
            continue
        if earliest <= day <= end:
            matches.append((match.start(), match.end()))

    if not matches and recent:
        matches = [(m.start(), m.end()) for m in PROSPECTIVE.finditer(body)][:1]
    excerpts = [body[:600]] if recent or matches else []
    last_end = -1
    for start, stop in matches:
        left, right = max(0, start - 180), min(len(body), stop + 300)
        if left < last_end:
            continue
        excerpts.append(body[left:right])
        last_end = right
        if len(excerpts) == 4:
            break
    if recent:
        agenda = re.search(r"\b(?:the agenda|coming up|economic calendar|due tomorrow|scheduled for tomorrow)\b", body, re.I)
        if agenda:
            passage = body[max(0, agenda.start() - 80):agenda.end() + 1000]
            if not any(passage in text or text in passage for text in excerpts):
                excerpts.append(passage)
    return excerpts


STOP = set('a an the and or to of in on at for with from by as is are was were be been being this that these those it its their our has have had will would may can could should into about after before through until under over up out new plan plans planned scheduled expected start starts starting take takes taking effect effective date dates event events announced announce announces announcement implement implementation end ends ending january february march april june july august september october november december jan feb mar apr jun jul aug sep oct nov dec'.split())
CHANGE = re.compile(r'\b(?:delay\w*|postpon\w*|cancel\w*|reschedul\w*|extend\w*|approv\w*|passed|passes|replac\w*|scrapp\w*|revers\w*|suspend\w*|reopen\w*|resum\w*|restor\w*|conclud\w*|ended|resolved|completed|terminated|no longer)\b',re.I)
def tokens(text):
 return {word[:7] if len(word)>7 else word for word in re.findall(r'[a-z][a-z0-9]*',text.lower()) if len(word)>1 and word not in STOP}
def retrieve_updates(events,catalog,timing,per_event=5):
 queries={e['event_id']:tokens(e['title']+' '+e['event_key']+' '+e.get('summary','')) for e in events};subjects={e['event_id']:tokens(e['event_key'].split('|')[0]) for e in events};vocab=set().union(*queries.values()) if queries else set()
 matched={ref:tokens(s['_source_text']) & vocab for ref,s in catalog.items()};df=Counter(token for terms in matched.values() for token in terms)
 weights={token:math.log(1+len(catalog)/(1+df[token])) for token in vocab};start=datetime.fromisoformat(timing['start_utc']);end=datetime.fromisoformat(timing['end_utc']);span=max(1,(end-start).total_seconds())
 candidates={key:[] for key in queries}
 for ref,source in catalog.items():
  possible=[key for key,q in queries.items() if len(q & matched[ref])>=min(2,len(q)) and (not subjects[key] or subjects[key] & matched[ref])]
  if not possible:continue
  body=source['_source_text'];fresh=1+.5*max(0,min(1,(datetime.fromisoformat(source['published_at'])-start).total_seconds()/span))
  for offset in range(0,len(body),500):
   passage=body[offset:offset+900]
   if len(passage)<40:continue
   terms=tokens(passage);change=1.8 if CHANGE.search(passage) else 1
   for key in possible:
    overlap=queries[key] & terms
    if len(overlap)<min(3,len(queries[key])) or (subjects[key] and not subjects[key] & terms):continue
    score=sum(weights[t] for t in overlap)*fresh*change
    candidates[key].append((score,source['published_at'],ref,offset,passage))
 result=[]
 for key,items in candidates.items():
  selected=[];counts=Counter()
  ranked=sorted(items,reverse=True)
  changes=[item for item in ranked if CHANGE.search(item[4])]
  ordered=changes[:8]+ranked
  for score,published,ref,offset,passage in ordered:
   if counts[ref]>=2 or any(old_ref==ref and abs(offset-old_offset)<700 for old_ref,old_offset in selected):continue
   selected.append((ref,offset));counts[ref]+=1;result.append({'event_id':key,'source_id':ref,'published_at':published,'passage':passage,'retrieval_score':round(score,6)})
   if len(selected)>=per_event:break
 return result
