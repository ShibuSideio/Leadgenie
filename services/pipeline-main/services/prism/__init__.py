"""
Pipeline-main — PRISM services package.

Contains:
  - ``OperatingModeRouter``   — classifies URLs into processing modes
  - ``WalledGardenHook``      — Serper triangulation for social/UGC
  - ``GeneralDomainHook``     — httpx DOM scrape with WAF fallback
  - ``B2B2CIntermediaryFinder`` — distributor/reseller search
  - ``PrismPipeline``         — orchestrator composing all four

These classes are extracted verbatim from the monolith and re-exported
from this package to maintain a clean import surface.
"""
from pipeline_main.services.prism.engine import (  # noqa: F401
    OperatingModeRouter,
    WalledGardenHook,
    GeneralDomainHook,
    B2B2CIntermediaryFinder,
    PrismPipeline,
)
