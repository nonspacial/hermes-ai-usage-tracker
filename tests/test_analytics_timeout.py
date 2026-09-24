"""Exercise the shipped read helper with a simulated slow transport."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_slow_analytics_reads_share_a_long_budget_but_status_stays_short():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for shipped JavaScript helper')
    source = (Path(__file__).resolve().parents[1] / 'desktop/plugin.js').read_text()
    helper = source[source.index('const pendingLedgerReads='):source.index('// The token is a filesystem hint')]
    script = """
const assert=require('node:assert/strict');
let calls=0;
async function scopedRead(path,options){
 calls++;
 const needed=path.startsWith('/ledger/status?')?100:57000;
 if((options?.timeoutMs??15000)<needed)throw Error('simulated transport timeout');
 return options?.timeoutMs;
}
""" + helper + """
(async()=>{
 for(const path of ['/ledger?start=0','/ledger?profile_scope=all','/ledger/skills?start=0']){
  const before=calls;
  const a=sharedLedgerRead(path),b=sharedLedgerRead(path);
  assert.equal(a,b);
  assert.equal(await a,120000);
  assert.equal(calls,before+1);
 }
 assert.equal(await sharedLedgerRead('/ledger/status?profile=infra',{timeoutMs:8000}),8000);
 assert.equal(pendingLedgerReads.size,0);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run([node, '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
