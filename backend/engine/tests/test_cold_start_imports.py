"""Engine cold start: READY does not wait for scikit-learn.

`import app.main` took about 2.1 s, and about 1.2 s of it was scikit-learn, imported at module
level by `app.performance` (through `app.profile`) and `app.intelligence` although nothing
needs it until a model is fitted or a similarity is scored. Those imports now live where they
are used, and the worker warms them in the background right after READY, next to the services
client, so neither READY nor the first draft pays for them.
"""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]


def test_importing_app_main_does_not_import_sklearn(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("STARLING_")}
    env.update({"STARLING_APP_DIR": str(tmp_path / "tenant"), "STARLING_RESEARCH": "off",
                "STARLING_DISABLE_WARM": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    code = "import sys; import app.main; print('SKLEARN', 'sklearn' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], cwd=str(ENGINE_DIR), env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "SKLEARN False" in result.stdout, "importing app.main pulled scikit-learn in again"


def test_the_worker_warms_sklearn_after_ready():
    from app import worker

    source = inspect.getsource(worker._serve)
    assert source.index("READY") < source.index("_warm_sklearn"), "warmed after READY, never before"
    worker._warm_sklearn()
    assert "sklearn.ensemble" in sys.modules and "sklearn.feature_extraction.text" in sys.modules
