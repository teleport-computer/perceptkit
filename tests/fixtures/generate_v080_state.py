"""Print a genuine v0.8.0 state fixture; run from any PerceptKit git checkout.

No current identity helpers are used. The pinned release is loaded in a fresh
subprocess. Its output is checked into v080_workout_state.json for offline tests.
"""
import io
import subprocess
import sys
import tarfile
import tempfile

COMMIT = "7449ecd9fab9cec0ede3e38d9ead53877fdfbf43"
PROGRAM = '''
from dataclasses import asdict
from datetime import datetime, timezone
import json
from perceptkit import PerceptionKit, IngestContext
from perceptkit.conformance import InMemoryStorage
t = datetime(2026, 9, 6, 9, tzinfo=timezone.utc)
s = InMemoryStorage()
k = PerceptionKit(s)
report = {"schema_version": 1, "report_id": "v080-original", "producer": "ios",
          "observations": [{"signal": "health_workout", "signal_schema_version": 1,
            "occurred_at": t.isoformat(), "availability": "observed", "timezone": "UTC",
            "source_event_id": "legacy-workout", "value": {
              "workout_type": "running", "duration_minutes": 30}}]}
out = k.ingest(report, context=IngestContext("u", t))
assert len(out.applied) == 1 and not out.rejected
print(json.dumps({"release": "v0.8.0", "commit": "7449ecd9fab9cec0ede3e38d9ead53877fdfbf43",
    "report": report, "identities": sorted(s.identities),
    "observations": [asdict(x) for x in s.observations.values()],
    "current": [asdict(x) for x in s.current.values()],
    "aggregates": [asdict(x) for x in s.aggregates.values()],
    "receipts": [asdict(x) for x in s.reports.values()]}, default=str, indent=2))
'''

if __name__ == "__main__":
    archive = subprocess.check_output(["git", "archive", COMMIT, "src"])
    with tempfile.TemporaryDirectory(prefix="perceptkit-v080-fixture-") as directory:
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(directory, filter="data")
        subprocess.run([sys.executable, "-c",
                        "import sys; sys.path.insert(0, " + repr(directory + "/src") + ")\n" + PROGRAM],
                       check=True)
