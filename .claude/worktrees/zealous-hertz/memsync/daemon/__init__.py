"""
memsync daemon — optional always-on companion module.

Install with: pip install memsync[daemon]

Core memsync never imports from this package.
This module only imports from memsync core, never the other way around.
"""
from memsync import __version__

DAEMON_VERSION = __version__
