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
    path.write_text(text)
    print('Preview regenerated from packaged Desktop source.')

if __name__=='__main__':build()
