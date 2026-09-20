import os
os.environ['HERMES_USAGE_PRICING_OFFLINE']='1'
import importlib.util,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('_test_bootstrap',ROOT/'bootstrap.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
