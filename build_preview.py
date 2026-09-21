#!/usr/bin/env python3
"""Refresh the embedded plugin in the existing offline preview harness."""
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parent

def preview_source():
    source=(ROOT/'desktop/plugin.js').read_text()
    source=re.sub(r'^import\s+[\s\S]*?\s+from\s+[\'\"][^\'\"]+[\'\"]\s*\n','',source,flags=re.M)
    return source.replace('export default {','const plugin = {')

def build():
    path=ROOT/'preview.html';text=path.read_text()
    start=text.index('/**\n * AI Usage Tracker',text.index('window.addDemoEvent'))
    end=text.index('plugin.register({',start)
    text=text[:start]+preview_source()+'\n'+text[end:]
    # Canonical, synthetic scope fixtures stay outside the packaged source.
    marker='// BEGIN ALL PROFILES OFFLINE FIXTURES'
    finish='// END ALL PROFILES OFFLINE FIXTURES'
    if marker in text:
        lo=text.index(marker);hi=text.index(finish,lo)+len(finish)+1
        text=text[:lo]+text[hi:]
    fixture=(ROOT/'tests/ui/all_profiles_fixture.js').read_text()
    text=text.replace('plugin.register({',marker+'\n'+fixture+finish+'\nplugin.register({',1)
    text=text.replace("storage:{get:(k,d)=>d,set:()=>{}}", "storage:{get:(k,d)=>window.demoStored[k]??d,set:(k,v)=>{window.demoStored[k]=v}}")
    text=text.replace('setInterval(reload,options.refetchInterval)', 'setInterval(()=>{if(options.refetchIntervalInBackground||document.visibilityState!==\'hidden\')reload()},options.refetchInterval)')
    path.write_text(text)
    print('Preview regenerated from packaged Desktop source.')

if __name__=='__main__':build()
