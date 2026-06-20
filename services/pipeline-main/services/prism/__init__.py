"""
Pipeline-main — PRISM services package.

Contains:
  - ``OperatingModeRouter``   — classifies URLs into processing modes
  - ``WalledGardenHook``      — Serper triangulation for social/UGC
  - ``GeneralDomainHook``     — httpx DOM scrape with WAF fallback
  - ``B2B2CIntermediaryFinder`` — distributor/reseller search
  - ``PrismPipeline``         — orchestrator composing all four

V23 architecture: these classes live in ``services/prism_pipeline.py``
and are imported directly by ``api/routers/dispatch.py``.
This package init is intentionally empty — no re-exports needed.
"""
