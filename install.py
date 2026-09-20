#!/usr/bin/env python3
"""Preview installation by default. --apply writes only named plugin files.

Does not enable plugins, restart agents, alter config/auth, update Hermes or
change quota. Backups and receipts permit checksum-checked rollback.
"""
from __future__ import annotations
import argparse,hashlib,json,os,shutil,sys,tempfile,uuid
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent
NAME='ai-usage-tracker'
PLUGIN_FILES=('LICENSE','README.md','UPSTREAM_README.md','PRICING_SOURCES.md',
              'COST_CARDS_AND_CACHE_GROWTH.md','SESSION_CACHE_WRITES.md',
              '__init__.py','bootstrap.py','plugin.yaml')
PLUGIN_DIRS=('dashboard','desktop','ledger_runtime')
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def no_links(path):
    for p in (path,*path.parents):
        if p.is_symlink():raise ValueError(f'Refusing a symlink target/ancestor: {p}. Install into its real, intended Hermes home explicitly.')
def atomic(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+'.ledger-'+uuid.uuid4().hex[:8]+'.tmp')
    try:
        tmp.write_bytes(data);os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)

def plan(root):
    root=Path(root).expanduser().absolute()
    if not root.is_dir():raise ValueError(f'No Hermes home directory: {root}')
    entries=[]
    sources=[ROOT/name for name in PLUGIN_FILES]
    for name in PLUGIN_DIRS:
        sources.extend((ROOT/name).rglob('*'))
    for src in sorted(sources):
        if not src.is_file() or '__pycache__' in src.parts or src.suffix=='.pyc':continue
        rel=src.relative_to(ROOT);dest=root/'plugins'/NAME/rel
        no_links(dest);entries.append((src,dest))
    dest=root/'desktop-plugins'/NAME/'plugin.js';no_links(dest)
    entries.append((ROOT/'desktop/plugin.js',dest))
    return root,entries

def install(home,apply):
    root,entries=plan(home)
    print(f'\nHome: {root}\n{len(entries)} plugin files; quota credentials/config/state.db untouched.')
    for src,dest in entries:print(('REPLACE ' if dest.exists() else 'CREATE  ')+str(dest.relative_to(root)))
    if not apply:return
    backup=root/'usage-ledger-backups'/(datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')+'-'+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True,exist_ok=False,mode=0o700)
    receipt={'version':1,'home':str(root),'files':[],'status':'in_progress'}
    receipt_path=backup/'receipt.json'
    # Save complete rollback material before touching any plugin file.
    for src,dest in entries:
        rel=dest.relative_to(root);old=None
        if dest.exists():
            if not dest.is_file():raise ValueError(f'Not a regular file: {dest}')
            old=str(Path('files')/rel);p=backup/old;p.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,p)
        receipt['files'].append({'relative_path':str(rel),'backup':old,'old_sha256':sha(dest),'installed_sha256':sha(src)})
    atomic(receipt_path,(json.dumps(receipt,indent=2)+'\n').encode())
    try:
        for src,dest in entries:atomic(dest,src.read_bytes())
    except BaseException:
        print('Installation interrupted. Receipt: '+str(receipt_path),file=sys.stderr);raise
    receipt['status']='installed';atomic(receipt_path,(json.dumps(receipt,indent=2)+'\n').encode())
    print('\nInstalled. Rollback receipt: '+str(receipt_path))
    print('Enable ai-usage-tracker in this profile and restart its Python producers/gateway. Reload Desktop plugins.')

def rollback(path,apply):
    p=Path(path).expanduser().resolve();d=json.loads(p.read_text());root=Path(d['home'])
    changes=[]
    for item in d['files']:
        rel=Path(item['relative_path'])
        if rel.is_absolute() or '..' in rel.parts:raise ValueError('Unsafe receipt path.')
        dest=root/rel;no_links(dest)
        current=sha(dest)
        if current not in (item['installed_sha256'],item['old_sha256']):raise ValueError(f'File changed since installation; refusing to overwrite: {dest}')
        old=None
        if item['backup']:
            b=p.parent/item['backup']
            if not b.resolve().is_relative_to(p.parent):raise ValueError('Unsafe backup path.')
            if sha(b)!=item['old_sha256']:raise ValueError('Backup checksum failed.')
            old=b
        changes.append((dest,old));print(('RESTORE ' if old else 'REMOVE  ')+str(dest))
    if apply:
        for dest,old in changes:
            if old:atomic(dest,old.read_bytes())
            else:dest.unlink(missing_ok=True)
        print('Plugin files restored. Restart producer/gateway processes. Ledger data has NOT been deleted.')

def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--home',action='append',help='Explicit Hermes home; repeat for multiple homes.')
    a.add_argument('--all-profiles',action='store_true',help='Existing default and directories under its profiles/.')
    a.add_argument('--apply',action='store_true');a.add_argument('--rollback',type=Path)
    args=a.parse_args()
    try:
        if args.rollback:rollback(args.rollback,args.apply);return 0
        default=Path(os.environ.get('HERMES_HOME',str(Path.home()/'.hermes'))).expanduser()
        homes=[Path(p) for p in args.home] if args.home else [default]
        if args.all_profiles:
            profiles=default/'profiles'
            if profiles.is_dir():homes += [p for p in sorted(profiles.iterdir()) if p.is_dir()]
        unique=list(dict.fromkeys(str(p.expanduser().absolute()) for p in homes))
        for home in unique:plan(home)
        for home in unique:install(home,args.apply)
        if not args.apply:print('\nPLAN ONLY. Re-run the same command with --apply to install.')
        return 0
    except (OSError,ValueError,KeyError) as e:print('ERROR: '+str(e),file=sys.stderr);return 1
if __name__=='__main__':raise SystemExit(main())
