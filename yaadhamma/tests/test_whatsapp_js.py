"""Run the WhatsApp DOM-extractor tests (Node harness over fixture DOMs).

The extractors in src/whatsapp_extractors.js hold every WhatsApp-Web
selector; these tests guard the extraction logic against regressions when
selectors are edited. Skipped where Node is unavailable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_HARNESS = _HERE / "js" / "wa_extractors.test.cjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_whatsapp_extractors_js() -> None:
    result = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
