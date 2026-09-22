"""Keep relative UI and settings paths beside the portable executable."""
import os
import sys

if getattr(sys, 'frozen', False):
    os.chdir(os.path.dirname(sys.executable))
