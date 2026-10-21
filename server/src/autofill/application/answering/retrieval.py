"""Local evidence routing; unanswerable retrieval falls back to exhaustive matching."""
from collections import defaultdict,Counter
import math,re


def terms(text):
 text=text.casefold();result=set(re.findall(r'[a-z][a-z0-9_-]{1,}|\d+(?:\.\d+)?',text))
 for run in re.findall(r'[\u3400-\u9fff]+',text):
  result.update(run[i:i+2] for i in range(len(run)-1))
 return result


def route_questions(questions,source_chunks,limit=12):
 index=defaultdict(set)
 for number,chunk in enumerate(source_chunks):
  tokens=set()
  for span in chunk:
   tokens.update(terms(span.text))
   context=span.locator.get('context',{})
   tokens.update(terms(context.get('heading','')))
  for token in tokens:index[token].add(number)
 result={}
 for question in questions:
  score=Counter()
  for token in terms(question.text+' '+' '.join(question.options)):
   matches=index.get(token,())
   for number in matches:score[number]+=1+math.log((len(source_chunks)+1)/(len(matches)+1))
  if not score:result[question.id]=set(range(len(source_chunks)));continue
  ranked=sorted(score,key=lambda number:(-score[number],number))[:limit]
  selected=set(ranked)
  # Neighbours preserve nearby applicability conditions at structural boundaries.
  for number in ranked[:4]:
   selected.update(i for i in (number-1,number+1) if 0<=i<len(source_chunks))
  result[question.id]=selected
 return result
