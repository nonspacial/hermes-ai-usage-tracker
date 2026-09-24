"""Namespace-relative API routes attached to the user's original quota router."""
from __future__ import annotations
import asyncio, csv, io, json, math, os, socket, sqlite3, time, uuid
from contextlib import closing
from pathlib import Path
from fastapi import HTTPException, Query, WebSocket, WebSocketDisconnect
from .storage import Store
from .pricing import start_worker
from .analytics_reload import AnalyticsRuntime, RestartRequired

def add_routes(router,resolve_profile,server_home):
    from . import aggregate

    def scope(value):
        if value not in ('selected', 'all'):
            raise HTTPException(400, 'Invalid profile scope.')
        return value == 'all'

    def readonly(value):
        if scope(value):
            raise HTTPException(409, 'All profiles is read-only; select one profile for this action.')

    @router.get('/ledger/profiles')
    def local_profiles():
        return aggregate.public_inventory(aggregate.discover(server_home()))

    @router.get('/ledger/change-token')
    def change_token(profile:str='', profile_scope:str='selected'):
        # A cheap hint, not a durable revision or a replacement for periodic
        # reconciliation. Do not instantiate Store or connect to source SQLite.
        from .change_check import check
        all_profiles = scope(profile_scope)
        root = None if all_profiles else check_profile(profile)
        return check(aggregate.discover(server_home()), selected_root=root)

    analytics = AnalyticsRuntime()

    def check_profile(profile):
        root,resolved,error=resolve_profile(profile)
        if error:raise HTTPException(404,error)
        return root or server_home()

    @router.get('/ledger/analytics')
    def analytics_info(profile:str='',profile_scope:str='selected'):
        if scope(profile_scope):return dict(analytics.info(), profile_scope='all', read_only=True)
        check_profile(profile)
        return analytics.info()

    @router.post('/ledger/analytics/reload')
    def reload_analytics(profile:str='',profile_scope:str='selected'):
        readonly(profile_scope)
        check_profile(profile)
        try:return analytics.reload()
        except RestartRequired as exc:raise HTTPException(409,str(exc)) from exc
        except Exception as exc:
            raise HTTPException(503,'Analytics reload failed validation; previous code remains active.') from exc

    def getstore(profile):
        root,resolved,error=resolve_profile(profile)
        if error:raise HTTPException(404,error)
        s=Store(root or server_home());start_worker(s);return s

    def check_bucket(bucket_start, bucket_end):
        if (bucket_start is None) != (bucket_end is None):
            raise HTTPException(400, 'Both bucket bounds are required.')
        if bucket_start is not None and (not math.isfinite(bucket_start) or not math.isfinite(bucket_end)
                                         or bucket_start < 0 or bucket_end <= bucket_start):
            raise HTTPException(400, 'Invalid time bucket.')

    @router.get('/ledger/status')
    def recorder_status(profile:str='',profile_scope:str='selected'):
        if scope(profile_scope):
            return {'profile_scope':'all','read_only':True,'refresh_mode':'polling',
                    'status':'unavailable','reason':'Recorder health is profile-specific; select a profile.'}
        # This endpoint never starts a producer, fetches prices, reads token
        # counters, redeems a reset, or restarts a running gateway.
        from .connection import summarize_health
        root,resolved,error=resolve_profile(profile)
        if error:raise HTTPException(404,error)
        target=Path(root or server_home())
        if not (target/'usage-ledger'/'events.sqlite3').is_file():
            return summarize_health([])
        try:
            with closing(sqlite3.connect((target/'usage-ledger'/'events.sqlite3').resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as connection:
                connection.row_factory=sqlite3.Row
                rows=[dict(row) for row in connection.execute('SELECT process,updated,data FROM health')]
            return summarize_health(rows)
        except Exception as exc:
            # Do not leak exception strings (paths, account details) to the UI.
            raise HTTPException(503,'Recorder status temporarily unavailable') from exc

    @router.get('/ledger')
    def ledger(profile:str='',start:float=0,end:float|None=None,provider:str='',session:str='',offset:int=0,limit:int=200,test_id:str='',agent:str='',project:str='',session_scope:str='exact',subagent:str='',model:str='',model_provider:str='',bucket_start:float|None=None,bucket_end:float|None=None,profile_scope:str='selected',view:str|None=None,group:str|None=None):
        if offset<0 or not 1<=limit<=2000:raise HTTPException(400,'Invalid pagination.')
        if not math.isfinite(start) or start<0 or end is not None and (not math.isfinite(end) or end<start):
            raise HTTPException(400,'Invalid time window.')
        check_bucket(bucket_start,bucket_end)
        try:
            from .projection import selected_fields
            selected_fields(view,group)
            if scope(profile_scope):
                return aggregate.ledger(analytics, aggregate.discover(server_home()), start=start,end=end,
                    provider=provider,session=session,offset=offset,limit=limit,test_id=test_id,agent=agent,
                    project=project,session_scope=session_scope,subagent=subagent,model=model,
                    model_provider=model_provider,bucket_start=bucket_start,bucket_end=bucket_end,view=view,group=group)
            root=check_profile(profile)
            return analytics.read(root,start,end,provider,offset,limit,session,agent,project,session_scope,subagent,
                                  model,model_provider,test_id=test_id,bucket_start=bucket_start,bucket_end=bucket_end,view=view,group=group,profile_key=profile)
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.post('/ledger/refresh')
    def refresh_ledger(body:dict,profile:str='',start:float=0,end:float|None=None,
                       provider:str='',session:str='',offset:int=0,limit:int=200,
                       agent:str='',project:str='',session_scope:str='exact',subagent:str='',
                       model:str='',model_provider:str='',profile_scope:str='selected',
                       view:str='overview',group:str='time',test_id:str='',
                       bucket_start:float|None=None,bucket_end:float|None=None):
        if offset<0 or not 1<=limit<=2000 or not math.isfinite(start) or start<0 or \
                end is not None and (not math.isfinite(end) or end<start):
            raise HTTPException(400,'Invalid analytics refresh window or pagination.')
        check_bucket(bucket_start,bucket_end)
        try:
            from .projection import selected_fields
            selected_fields(view,group)
            if scope(profile_scope):
                return aggregate.ledger(analytics,aggregate.discover(server_home()),start=start,end=end,
                    provider=provider,session=session,offset=offset,limit=limit,test_id=test_id,
                    agent=agent,project=project,session_scope=session_scope,subagent=subagent,
                    model=model,model_provider=model_provider,bucket_start=bucket_start,
                    bucket_end=bucket_end,view=view,group=group)
            token=body.get('resume_token') if isinstance(body,dict) else None
            root=check_profile(profile)
            args=(start,end,provider,offset,limit,session,agent,project,session_scope,
                  subagent,model,model_provider)
            if not token:
                return analytics.read(root,*args,test_id=test_id,bucket_start=bucket_start,
                                      bucket_end=bucket_end,view=view,group=group,profile_key=profile)
            return analytics.refresh(root,*args,resume_token=token,view=view,group=group,
                                     test_id=test_id,bucket_start=bucket_start,bucket_end=bucket_end,profile_key=profile)
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from exc

    @router.get('/ledger/skills')
    def skills_usage(profile:str='',start:float=0,end:float|None=None,provider:str='',session:str='',
                     session_scope:str='exact',agent:str='',project:str='',subagent:str='',test_id:str='',
                     model:str='',model_provider:str='',bucket_start:float|None=None,bucket_end:float|None=None,
                     skill:str='',offset:int=0,limit:int=200,profile_scope:str='selected'):
        from .skills import read
        check_bucket(bucket_start,bucket_end)
        try:
            if scope(profile_scope):
                return aggregate.skills(aggregate.discover(server_home()), start=start,end=end,provider=provider,
                    session=session,session_scope=session_scope,agent=agent,project=project,subagent=subagent,
                    test_id=test_id,model=model,model_provider=model_provider,bucket_start=bucket_start,
                    bucket_end=bucket_end,skill=skill,offset=offset,limit=limit)
            root=check_profile(profile)
            return read(root,start=start,end=end,provider=provider,session=session,session_scope=session_scope,
                        agent=agent,project=project,subagent=subagent,test_id=test_id,model=model,
                        model_provider=model_provider,bucket_start=bucket_start,bucket_end=bucket_end,skill=skill,
                        offset=offset,limit=limit)
        except ValueError as exc:raise HTTPException(400,str(exc)) from exc
        except (sqlite3.Error,OSError):raise HTTPException(503,'Skills observations temporarily unavailable.')

    @router.post('/ledger/rates')
    def rate(body:dict,profile:str='',profile_scope:str='selected'):
        readonly(profile_scope)
        raise HTTPException(410,'Manual rate entry retired. Prices are supplied by provider catalogs.')

    @router.post('/ledger/pricing/refresh')
    def refresh_prices(profile:str='',profile_scope:str='selected'):
        readonly(profile_scope)
        s=getstore(profile);start_worker(s,force=True)
        return {'status':'queued','message':'Public provider catalog refresh queued. No inference calls.'}

    @router.post('/ledger/tests')
    def tests(body:dict,profile:str='',profile_scope:str='selected'):
        readonly(profile_scope)
        try:return getstore(profile).test(body.get('action'),body.get('label',''),body.get('id'))
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.websocket('/ledger/events')
    async def events(ws:WebSocket,profile:str='',profile_scope:str='selected'):
        if profile_scope != 'selected':
            await ws.close(code=1008, reason='Use polling for All profiles.');return
        # Authentication/origin policy remains the host gateway's policy. No extra
        # network listener or credentials are created by this plugin.
        try:folder=Path(check_profile(profile))/'usage-ledger'
        except HTTPException:await ws.close(code=1008);return
        await ws.accept()
        sock=None;path=None
        try:
            if hasattr(socket,'AF_UNIX') and folder.is_dir():
                path=folder/f'notify-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock'
                if len(str(path).encode())<100:
                    sock=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);sock.setblocking(False);sock.bind(str(path));os.chmod(path,0o600)
            await ws.send_json({'type':'connected','mode':'native-events' if sock else 'display-refresh-fallback'})
            while True:
                if sock:
                    try:
                        await asyncio.wait_for(asyncio.get_running_loop().sock_recv(sock,1024),timeout=20)
                        # Coalesce many provider completions into one repaint hint.
                        await asyncio.sleep(.15)
                        while True:
                            try:sock.recv(1024)
                            except BlockingIOError:break
                        await ws.send_json({'type':'changed'})
                    except asyncio.TimeoutError:await ws.send_json({'type':'heartbeat'})
                else:
                    await asyncio.sleep(20);await ws.send_json({'type':'heartbeat'})
        except (WebSocketDisconnect,RuntimeError,OSError):pass
        finally:
            if sock:sock.close()
            if path:
                try:path.unlink(missing_ok=True)
                except OSError:pass
