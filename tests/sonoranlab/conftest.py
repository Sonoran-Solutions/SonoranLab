"""Test jobs keep their synthetic profiles under the temp dir: declare it a read root, as a real deployment would.

Review refuses a profile outside the read roots unless git can recover it. Tests of that confinement set their own
SONORANLAB_READ_ROOTS.
"""

import os
import tempfile

os.environ.setdefault("SONORANLAB_READ_ROOTS", tempfile.gettempdir())
