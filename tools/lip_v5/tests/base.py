"""
Shared test base.  THE SUITE MUST NEVER PAGE AND MUST NEVER WRITE OUTSIDE ITS TMPDIR.

Two real incidents this week: a unit suite fired a push to a phone, and a unit suite wrote
outside tmp.  Both are structural failures, so the guards here are structural too — not a
convention every test author has to remember:

  * `runtime.set_write_roots([tmpdir])` makes any write outside the tmpdir raise.
  * `runtime.set_alert_sink(...)` captures every page; `NTFY_DISABLE` is set as a second,
    independent belt.
  * `runtime.set_live(False)` is the default, and `runtime.http()` raises while not live, so
    no test can reach the network even by accident.

`test_no_external_effects.py` asserts all three, so a regression in the guards themselves
fails the suite rather than going quiet.
"""

import os
import shutil
import tempfile
import unittest

from .. import runtime as R


class LipTestCase(unittest.TestCase):
    def setUp(self):
        os.environ["NTFY_DISABLE"] = "1"
        self.tmp = tempfile.mkdtemp(prefix="lip_v5_test_")
        self.alerts = []
        self.logs = []
        R.set_live(False)
        R.set_write_roots([self.tmp])
        R.set_alert_sink(lambda name, msg: self.alerts.append((name, msg)))
        R.set_log_sink(self.logs.append)
        self.addCleanup(self._teardown)

    def _teardown(self):
        R.set_alert_sink(None)
        R.set_log_sink(None)
        R.set_write_roots(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def logs_of(self, kind):
        return [r for r in self.logs if r.get("t") == kind]
