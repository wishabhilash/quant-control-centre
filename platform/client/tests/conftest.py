"""Make the client importable without installing it.

``sys.path`` gains ``src`` so ``import quant_client`` resolves to the in-tree
package. Tests inject a fake Redis client (``client=``), so neither the
``redis`` package nor a live server is required — the strategy-side wire
protocol is what is under test, not Redis itself.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
