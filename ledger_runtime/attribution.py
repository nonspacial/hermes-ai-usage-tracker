"""Content-free session/project attribution at native event boundaries.

Only id/source/parent_session_id/git_repo_root/cwd are read from state.db, in a
read-only transaction. No token counters, messages, titles or configurations.
No process cwd fallback: an unattributed project stays unattributed.
"""
from __future__ import annotations
import hashlib, json, posixpath, sqlite3, time
from pathlib import Path

FIELDS=('agent_kind','parent_session_id','root_session_id','session_lineage','lineage_complete',
        'subagent_id','parent_subagent_id','agent_role','parent_turn_id','project_id',
        'project_label','project_path','project_source','attribution_source')
STATE_FIELDS=('id','source','parent_session_id','git_repo_root','cwd')
PRIMARY_PLATFORMS={'desktop','tui','cli','cron','discord','telegram','slack','whatsapp','signal','matrix','gateway','web'}

def clean(v,limit=1024):
    return v[:limit] if isinstance(v,str) else ''

def state_rows(root,sid):
    """Bounded metadata read. Schema absence/locks never prevent an inference."""
    path=Path(root)/'state.db';result=[]
    if not sid or not path.is_file():return result
    con=None
    try:
        con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.05)
        con.row_factory=sqlite3.Row
        con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
        cols={r[1] for r in con.execute('PRAGMA table_info(sessions)')}
        if 'id' not in cols:return []
        chosen=[f for f in STATE_FIELDS if f in cols]
        sql='SELECT '+','.join('"'+f+'"' for f in chosen)+' FROM sessions WHERE id=?'
        seen=set()
        while sid and sid not in seen and len(seen)<64:
            seen.add(sid);row=con.execute(sql,(sid,)).fetchone()
            if not row:break
            data=dict(row);result.append(data);sid=data.get('parent_session_id')
    except (sqlite3.Error,OSError):return []
    finally:
        if con:con.close()
    return result

def from_state(row):
    source=clean(row.get('source'),80)
    out={'session_id':clean(row.get('id')),'platform':source,'attribution_source':'session_metadata'}
    # Parent != subagent: compactions/branches may also have a parent.
    if source=='subagent':out['agent_kind']='subagent'
    elif source in PRIMARY_PLATFORMS:out['agent_kind']='primary'
    if 'parent_session_id' in row:
        out['parent_session_id']=clean(row.get('parent_session_id'))
        out['parent_known']=True
    for key in ('git_repo_root','cwd'):
        if clean(row.get(key)):
            out[key]=clean(row[key],2048)
    return out

def persist(c,meta):
    sid=clean(meta.get('session_id'))
    if not sid:return
    old=c.execute('SELECT data FROM session_context WHERE session_id=?',(sid,)).fetchone()
    data=json.loads(old[0]) if old else {}
    for k,v in meta.items():
        if k=='session_id' or v is None or (v=='' and k!='parent_session_id'):continue
        # Never downgrade explicit delegation from a later generic platform tag.
        if k=='agent_kind' and data.get(k)=='subagent' and v!='subagent':continue
        if k=='parent_session_id' and data.get(k) and not v:continue
        if k=='attribution_source' and data.get(k)=='subagent_lifecycle' and v!='subagent_lifecycle':continue
        data[k]=v
    if old and data==json.loads(old[0]):return
    c.execute('INSERT INTO session_context VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET updated=excluded.updated,data=excluded.data',(sid,time.time(),json.dumps(data,separators=(',',':'))))

def resolve(c,sid,platform=''):
    """Resolve stored ancestry; never confuse a guessed top ancestor with a root."""
    chain=[];seen=set();cur=sid;complete=False
    while cur and cur not in seen and len(chain)<64:
        seen.add(cur)
        row=c.execute('SELECT data FROM session_context WHERE session_id=?',(cur,)).fetchone()
        if not row:break
        meta=json.loads(row[0]);chain.append((cur,meta))
        parent=meta.get('parent_session_id')
        if not parent:
            complete=bool(meta.get('parent_known'))
            break
        cur=parent
    own=chain[0][1] if chain else {}
    kind=own.get('agent_kind') or ('subagent' if platform=='subagent' else 'primary' if platform in PRIMARY_PLATFORMS else 'unknown')
    lineage=[s for s,m in chain[1:]]
    if chain and cur and cur not in [s for s,m in chain]:lineage.append(cur)
    result={'agent_kind':kind,'parent_session_id':own.get('parent_session_id') or None,
      'root_session_id':chain[-1][0] if complete and chain else None,'session_lineage':lineage,
      'lineage_complete':complete,'attribution_source':own.get('attribution_source') or ('platform' if kind!='unknown' else 'unavailable')}
    for k in ('subagent_id','parent_subagent_id','agent_role','parent_turn_id'):
        result[k]=own.get(k) or None
    # Delegates use the nearest owning parent's project when recorded. Child
    # working directories may be temporary worktrees or a generic launcher cwd.
    project_chain=chain[1:]+chain[:1] if kind=='subagent' and len(chain)>1 else chain
    candidates=[(owner,meta,key) for key in ('git_repo_root','cwd') for owner,meta in project_chain if meta.get(key)]
    for owner,meta,key in candidates:
        path=clean(meta[key],2048).replace('\\','/').rstrip('/') or '/'
        project_kind='repository' if key=='git_repo_root' else 'working_directory'
        result.update(project_id=hashlib.sha256((project_kind+':'+path).encode()).hexdigest()[:24],
          project_label=posixpath.basename(path) or path,project_path=path,
          project_source=('parent_' if owner!=sid else '')+project_kind)
        break
    else:result.update(project_id=None,project_label=None,project_path=None,project_source='unavailable')
    return result

def capture(store,sid,platform='',explicit=None):
    if not sid:return {}
    rows=state_rows(store.root,sid)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        for row in reversed(rows):persist(c,from_state(row))
        base={'session_id':sid}
        if platform:
            base['platform']=clean(platform,80)
            if platform=='subagent':base['agent_kind']='subagent'
            elif platform in PRIMARY_PLATFORMS:base['agent_kind']='primary'
        if explicit:base.update(explicit)
        persist(c,base)
        return resolve(c,sid,platform)

def attach(c,data):
    sid=data.get('session_id')
    if not sid:return data
    ctx=resolve(c,sid,data.get('platform',''))
    for k,v in ctx.items():
        # Metadata first discovered later may fill a gap; do not silently move
        # already-attributed historical requests to a different project.
        missing=data.get(k) in (None,'','unknown','unavailable') if k!='session_lineage' else not data.get(k)
        if missing or (k=='agent_kind' and v=='subagent') or (k=='lineage_complete' and v) or (k=='session_lineage' and len(v)>len(data.get(k) or [])):
            data[k]=v
    return data

def lifecycle(store,kind,kw):
    sid=clean(kw.get('child_session_id'))
    if not sid:return
    parent=clean(kw.get('parent_session_id'))
    if parent:capture(store,parent)
    safe={'session_id':sid,'agent_kind':'subagent','attribution_source':'subagent_lifecycle',
      'parent_session_id':parent,'parent_known':bool(parent),'parent_turn_id':clean(kw.get('parent_turn_id')),
      'agent_role':clean(kw.get('child_role'),80)}
    if kind=='start':
        safe.update(subagent_id=clean(kw.get('child_subagent_id')),parent_subagent_id=clean(kw.get('parent_subagent_id')),spawn_observed_at=time.time())
    else:
        safe.update(exit_observed_at=time.time(),exit_status=clean(kw.get('child_status'),80))
        duration=kw.get('duration_ms')
        if isinstance(duration,(int,float)) and duration>=0:safe['duration_ms']=duration
    capture(store,sid,'subagent',safe)
    with store.db() as c:
        store.event(c,'subagent_'+kind,sid,safe)
        # Late lifecycle notifications can fill unknown attribution without
        # importing parent's totals, repricing, or introducing another request.
        for row in c.execute('SELECT id,data FROM requests WHERE session_id=?',(sid,)).fetchall():
            data=json.loads(row['data']);attach(c,data)
            c.execute('UPDATE requests SET data=? WHERE id=?',(json.dumps(data,separators=(',',':')),row['id']))
    from .storage import notify
    notify(store.folder)
