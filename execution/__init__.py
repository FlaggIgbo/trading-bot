"""
Execution layer: order management, placement, and fill reconciliation.

This is the OUTPUT layer of the ML pipeline — it takes trade recommendations
from the models layer and converts them into actual exchange orders.

Submodules:
    execution.engine              - Order sync, cancel, and placement logic
    execution.fill_reconciliation - Post-trade fill matching and accounting
"""
