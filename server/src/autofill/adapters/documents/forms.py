"""Legacy Word form fields: fill permitted results while preserving protection."""
from .documents import W, node_paths
from .extract import text

def legacy_fields(root):
 paths=node_paths(root);parents={id(child):parent for parent in root.iter() for child in parent}
 for paragraph in root.iter(W+'p'):
  nodes=list(paragraph.iter());active=None;separated=False
  for i,node in enumerate(nodes):
   if node.tag==W+'fldChar':
    typ=node.get(W+'fldCharType')
    if typ=='begin':
     ff=node.find(W+'ffData');active={'ff':ff,'texts':[],'start':i} if ff is not None else None;separated=False
    elif typ=='separate':separated=True
    elif typ=='end' and active is not None:
     ff=active['ff'];enabled=ff.find(W+'enabled')
     if enabled is not None and enabled.get(W+'val') in ('0','false'):active=None;continue
     checkbox=ff.find(W+'checkBox');dropdown=ff.find(W+'ddList');texts=active['texts']
     existing=''.join(n.text or '' for n in texts)
     if (not texts and checkbox is None) or (checkbox is None and existing.strip()):active=None;continue
     if checkbox is not None:
      check=checkbox.find(W+'checked');check=check if check is not None else checkbox.find(W+'default')
      if check is not None and check.get(W+'val') in ('1','true'):active=None;continue
     before=''.join(n.text or '' for n in nodes[:active['start']] if n.tag==W+'t').strip()
     after=''.join(n.text or '' for n in nodes[i+1:] if n.tag==W+'t').strip()
     context='';cell=paragraph
     while id(cell) in parents and cell.tag!=W+'tc':cell=parents[id(cell)]
     if cell.tag==W+'tc':
      row=parents.get(id(cell));cells=list(row) if row is not None else []
      context=' / '.join(text(c).strip() for c in cells[:cells.index(cell)] if c.tag==W+'tc' and text(c).strip())
     name=ff.find(W+'name');name=name.get(W+'val','') if name is not None else ''
     label=' / '.join(dict.fromkeys(v for v in (context,before[-160:],after[:100]) if v)) or name
     kind='boolean' if checkbox is not None else 'single_choice' if dropdown is not None else 'text'
     options=['Yes','No'] if checkbox is not None else [n.get(W+'val','') for n in dropdown.findall(W+'listEntry')] if dropdown is not None else []
     yield label,{'type':'docx_legacy','path':paths[id(ff)],'text_paths':[paths[id(n)] for n in texts],'checkbox':checkbox is not None,'field_name':name},kind,options,not bool(context or before) or checkbox is not None
     active=None
   elif node.tag==W+'t' and active is not None and separated:active['texts'].append(node)
