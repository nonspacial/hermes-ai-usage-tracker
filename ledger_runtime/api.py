"""Namespace-relative API routes attached to the user's original quota router."""
from __future__ import annotations
import asyncio, csv, io, json, os, socket, time, uuid
from pathlib import Path
from fastapi import HTTPException, Query, WebSocket, WebSocketDisconnect
from .storage import Store
from .pricing import start_worker

def add_routes(router,resolve_profile,server_home):
    def getstore(profile):
        root,resolved,error=resolve_profile(profile)
        if error:raise HTTPException(404,error)
        s=Store(root or server_home());start_worker(s);return s

    @router.get('/ledger/status')
    def recorder_status(profile:str=''):
        # This endpoint never starts a producer, fetches prices, reads token
        # counters, redeems a reset, or restarts a running gateway.
        from .connection import status, summarize_health
        root,resolved,error=resolve_profile(profile)
        if error:raise HTTPException(404,error)
        target=Path(root or server_home())
        if not (target/'usage-ledger'/'events.sqlite3').is_file():
            return summarize_health([])
        try:return status(Store(target))
        except Exception as exc:
            # Do not leak exception strings (paths, account details) to the UI.
            raise HTTPException(503,'Recorder status temporarily unavailable') from exc

    @router.get('/ledger')
    def ledger(profile:str='',start:float=0,end:float|None=None,provider:str='',session:str='',offset:int=0,limit:int=200,test_id:str='',agent:str='',project:str='',session_scope:str='exact',subagent:str=''):
        if offset<0 or not 1<=limit<=2000:raise HTTPException(400,'Invalid pagination.')
        try:
            s=getstore(profile)
            if test_id:start,end=s.test_window(test_id)
            return s.read(start,end,provider,offset,limit,session,agent,project,session_scope,subagent)
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.post('/ledger/rates')
    def rate(body:dict,profile:str=''):
        raise HTTPException(410,'Manual rate entry retired. Prices are supplied by provider catalogs.')

    @router.post('/ledger/pricing/refresh')
    def refresh_prices(profile:str=''):
        s=getstore(profile);start_worker(s,force=True)
        return {'status':'queued','message':'Public provider catalog refresh queued. No inference calls.'}

    @router.post('/ledger/tests')
    def tests(body:dict,profile:str=''):
        try:return getstore(profile).test(body.get('action'),body.get('label',''),body.get('id'))
        except ValueError as exc:raise HTTPException(400,str(exc))

    @router.websocket('/ledger/events')
    async def events(ws:WebSocket,profile:str=''):
        # Authentication/origin policy remains the host gateway's policy. No extra
        # network listener or credentials are created by this plugin.
        try:s=getstore(profile)
        except HTTPException:await ws.close(code=1008);return
        await ws.accept()
        sock=None;path=None
        try:
            if hasattr(socket,'AF_UNIX'):
                path=s.folder/f'notify-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock'
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
