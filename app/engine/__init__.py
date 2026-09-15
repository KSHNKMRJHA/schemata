"""FastAPI integration layer for the Schemata BOM analysis engine.

This package wraps the self-contained ``app.engine.bomiq`` engine (7-provider
BOM analysis, ingestion pipeline, scoring systems) so it can be driven from
FastAPI without blocking the event loop.

Public modules
--------------
``app.engine.bridge``   -- Engine lifecycle + async bridge helpers
``app.engine.routes``   -- FastAPI router exposing the BOM API
"""