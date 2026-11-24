import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from kairos.research.job import FEATURES, run
from kairos.research.learning import ModelRegistry


def test_job_insufficient_then_trains(tmp_path):
    reg = ModelRegistry(tmp_path / "m", b"k")
    d = tmp_path / "f" / "date=2026-11-24"
    d.mkdir(parents=True)
    rng = np.random.default_rng(0)
    rows, sets = [], {}
    for w in range(260):
        tk = f"KXBTC15M-W{w:04d}"
        z = rng.normal()
        sets[tk] = "yes" if z + rng.normal(0, 0.7) > 0 else "no"
        for k in range(5):
            r = {f: float(rng.normal()) for f in FEATURES}
            r.update(ts=w * 900.0 + k * 60, ticker=tk, seconds_left=900.0 - k * 60, z_to_strike=z, p_model=float(1 / (1 + np.exp(-1.5 * z))))
            rows.append(r)
    pq.write_table(pa.Table.from_pylist(rows), d / "p.parquet")
    small = run(tmp_path / "f", dict(list(sets.items())[:10]), reg)
    assert "insufficient" in small["status"]
    rep = run(tmp_path / "f", sets, reg)
    assert len(rep["trained"]) == 2 and all(not t["gates_passed"] for t in rep["trained"])  # never auto-promoted
