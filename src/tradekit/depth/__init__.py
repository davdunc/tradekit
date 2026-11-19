"""Machine-readable order-book depth capture (#9).

Order-book depth can't be backfilled: a session nobody recorded is lost. This package records
it as it happens, from whichever feed is available:

- ``events``: the normalized ``BookUpdate`` / ``Trade`` / ``Marker`` rows
- ``sink``: crash-safe Parquet output, partitioned by kind / date / source / symbol
- ``recorder``: source selection: ``auto`` (DAS, then IBKR Gateway) or one explicit source;
  Alpaca (level 1 only) runs only when explicitly selected
- ``sources``: the adapters

Run it with ``tradekit depth record``.
"""
