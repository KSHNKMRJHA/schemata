"""
The analysis engine: the single entry point that turns a BOM file into a
:class:`~bomiq.core.models.BomAnalysis`.

Pipeline
--------
::

    ingest -> deduplicate lookups -> enrich (providers, concurrent)
           -> match & complete -> compliance -> cost -> risk
           -> alternates (only where they help) -> health roll-up

Design points worth knowing:

* **Cancellable and observable.** Every stage reports progress through a
  callback and checks a cancel flag, so the UI stays responsive and a user can
  stop a 3000-line run.
* **Degrades, never fails.** A provider outage, a missing key, a malformed API
  response or an exception inside one line's analysis is captured and reported
  against that line; the rest of the BOM still completes.
* **Deterministic offline.** With no keys the offline catalogue drives the same
  pipeline, so behaviour and tests are identical minus the live data.
* **Alternates are earned, not automatic.** Extra provider calls only happen
  for lines where an alternate would actually change a decision.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from .analysis import alternates as alternates_mod
from .analysis import compliance as compliance_mod
from .analysis import cost as cost_mod
from .analysis import health as health_mod
from .analysis import match as match_mod
from .analysis import risk as risk_mod
from .config import Config, get_config
from .core.db import Database, PartCache, ProjectStore, ResponseCache, TemplateStore
from .core.errors import CancelledError
from .core.models import (
    AnalysisSummary, Bom, BomAnalysis, BomLine, Issue, LineResult, MatchKind,
    PartData, Severity,
)
from .ingest.pipeline import IngestOptions, Ingestor, IngestResult
from .providers.registry import LookupResult, ProviderRegistry
from .util import log
from .util.http import HttpClient
from .util.money import FxTable
from .util.text import clean, normalize_mpn
from .version import __version__

LOG = log.get("engine")

ProgressFn = Callable[["Progress"], None]


@dataclass
class Progress:
    """A progress event. Cheap to construct; emitted often."""

    stage: str
    message: str
    done: int = 0
    total: int = 0
    percent: float = 0.0
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage, "message": self.message, "done": self.done,
            "total": self.total, "percent": round(self.percent, 1),
            "elapsed_ms": self.elapsed_ms,
        }


STAGE_WEIGHTS = {
    "ingest": 8.0,
    "enrich": 55.0,
    "analyse": 17.0,
    "alternates": 15.0,
    "finalise": 5.0,
}


class CancelToken:
    """Thread-safe cancellation flag."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise CancelledError("Analysis cancelled by the user.")


class Engine:
    """Owns the database, provider registry and analysis settings."""

    def __init__(self, config: Config | None = None,
                 db: Database | None = None) -> None:
        self.config = config or get_config()
        log.setup(self.config.log_dir, self.config.settings.log_level)
        self.db = db or Database(self.config.db_path)
        self.response_cache = ResponseCache(
            self.db, enabled=self.config.settings.cache_enabled)
        self.part_cache = PartCache(
            self.db, ttl=self.config.settings.part_cache_ttl,
            enabled=self.config.settings.cache_enabled)
        self.templates = TemplateStore(self.db)
        self.projects = ProjectStore(self.db)
        self.ingestor = Ingestor(self.templates)
        self.http = HttpClient(
            rate_per_second=4.0,
            retries=self.config.settings.http_retries,
            timeout=self.config.settings.request_timeout,
            cache=self.response_cache,
        )
        self.fx = FxTable()

    # -- lifecycle -------------------------------------------------------- #

    def close(self) -> None:
        self.db.close()

    def registry(self, provider_ids: Sequence[str] | None = None,
                 credentials: dict[str, dict[str, str]] | None = None
                 ) -> ProviderRegistry:
        return ProviderRegistry(self.config, part_cache=self.part_cache,
                                http=self.http, provider_ids=provider_ids,
                                credential_overrides=credentials)

    # -- ingestion only --------------------------------------------------- #

    def ingest(self, path: str | Path, **options: Any) -> IngestResult:
        ingest_options = IngestOptions.from_settings(self.config.settings,
                                                     **options)
        return self.ingestor.ingest_file(path, ingest_options)

    def ingest_bytes(self, raw: bytes, name: str = "upload",
                     **options: Any) -> IngestResult:
        ingest_options = IngestOptions.from_settings(self.config.settings,
                                                     **options)
        return self.ingestor.ingest_bytes(raw, name=name,
                                          options=ingest_options)

    # -- full analysis ---------------------------------------------------- #

    def analyse_file(self, path: str | Path, *,
                     progress: ProgressFn | None = None,
                     cancel: CancelToken | None = None,
                     provider_ids: Sequence[str] | None = None,
                     credentials: dict[str, dict[str, str]] | None = None,
                     **ingest_options: Any) -> BomAnalysis:
        started = time.monotonic()
        emit = _emitter(progress, started)
        emit("ingest", f"Reading {Path(path).name}…", 0, 1)
        result = self.ingest(path, **ingest_options)
        emit("ingest", f"Read {result.bom.line_count} lines.", 1, 1)
        return self.analyse_bom(result.bom, progress=progress, cancel=cancel,
                                provider_ids=provider_ids, started=started,
                                credentials=credentials)

    def analyse_bytes(self, raw: bytes, name: str = "upload", *,
                      progress: ProgressFn | None = None,
                      cancel: CancelToken | None = None,
                      provider_ids: Sequence[str] | None = None,
                      credentials: dict[str, dict[str, str]] | None = None,
                      **ingest_options: Any) -> BomAnalysis:
        started = time.monotonic()
        emit = _emitter(progress, started)
        emit("ingest", f"Reading {name}…", 0, 1)
        result = self.ingest_bytes(raw, name=name, **ingest_options)
        emit("ingest", f"Read {result.bom.line_count} lines.", 1, 1)
        return self.analyse_bom(result.bom, progress=progress, cancel=cancel,
                                provider_ids=provider_ids, started=started,
                                credentials=credentials)

    def analyse_bom(self, bom: Bom, *, progress: ProgressFn | None = None,
                    cancel: CancelToken | None = None,
                    provider_ids: Sequence[str] | None = None,
                    credentials: dict[str, dict[str, str]] | None = None,
                    started: float | None = None) -> BomAnalysis:
        """Analyse an already-ingested BOM."""
        started = started or time.monotonic()
        cancel = cancel or CancelToken()
        emit = _emitter(progress, started)

        # A per-run copy of the settings. Mutating the shared Config.settings
        # here would leak this BOM's build quantity into every later analysis
        # and into concurrent requests, silently costing them at the wrong
        # volume, so the run gets its own immutable snapshot instead.
        settings = replace(self.config.settings)
        if bom.build_quantity and bom.build_quantity > 1:
            settings.build_quantity = bom.build_quantity

        analysis = BomAnalysis(bom=bom, engine_version=__version__)
        analysis.started_at = _utc_now()
        analysis.issues.extend(bom.issues)

        registry = self.registry(provider_ids, credentials=credentials)
        analysis.providers_used = registry.ids
        analysis.offline = registry.is_offline
        if registry.setup_errors:
            for provider_id, message in registry.setup_errors.items():
                analysis.warnings.append(f"{provider_id}: {message}")
        if analysis.offline:
            analysis.warnings.append(
                "Running on the offline catalogue: figures are synthetic "
                "placeholders, not live distributor data. Add an API key in "
                "Settings for real pricing, stock and lifecycle status.")

        # ---------------- enrich ---------------------------------------- #
        requests = [
            (line.mpn or line.distributor_pn, line.manufacturer)
            for line in bom.lines
            if (line.mpn or line.distributor_pn)
        ]
        unique_count = len({normalize_mpn(mpn) for mpn, _ in requests
                            if normalize_mpn(mpn)})
        emit("enrich",
             f"Looking up {unique_count} unique part(s) across "
             f"{len(registry.ids)} provider(s)…", 0, max(1, unique_count))

        lookups: dict[str, LookupResult] = {}
        if requests:
            def on_progress(done: int, total: int, mpn: str) -> None:
                emit("enrich", f"Looked up {mpn}", done, total)

            try:
                lookups = registry.lookup_many(
                    requests, max_workers=settings.max_workers,
                    progress=on_progress,
                    should_cancel=lambda: cancel.cancelled,
                )
            except Exception as exc:  # pragma: no cover - defensive
                LOG.exception("Enrichment failed")
                analysis.warnings.append(
                    f"Part lookup stopped early: {exc}. The BOM was still "
                    f"validated and any data already retrieved was used.")
        cancel.check()

        # ---------------- per-line analysis ------------------------------ #
        emit("analyse", "Scoring lines…", 0, max(1, len(bom.lines)))
        results: list[LineResult] = []
        for index, line in enumerate(bom.lines, start=1):
            cancel.check()
            try:
                results.append(
                    self._analyse_line(line, lookups, settings))
            except Exception as exc:  # pragma: no cover - defensive
                LOG.exception("Line %s failed", line.line_no)
                fallback = LineResult(line=line)
                fallback.issues.append(Issue(
                    code="line_analysis_failed",
                    message=f"This line could not be analysed: {exc}",
                    severity=Severity.ERROR, line_no=line.line_no,
                ))
                results.append(fallback)
            if index % 25 == 0 or index == len(bom.lines):
                emit("analyse", f"Scored {index} of {len(bom.lines)} lines",
                     index, len(bom.lines))
        analysis.results = results

        # ---------------- alternates ------------------------------------- #
        self._add_alternates(results, registry, emit, cancel, settings)

        # ---------------- risk (needs cost + alternates) ----------------- #
        emit("finalise", "Rolling up risk and health…", 0, 3)
        for result in results:
            result.risk = risk_mod.score_line(result, settings)
            result.issues.extend(risk_mod.risk_issues(result, settings))

        # ---------------- roll-up ---------------------------------------- #
        analysis.health = health_mod.compute(results, settings)
        emit("finalise", "Building the summary…", 1, 3)
        analysis.summary = self._summarise(bom, results, registry, settings)

        compliance_summary = compliance_mod.summarise(results, settings)
        analysis.summary.compliance_counts = {
            f"rohs_{key.lower().replace('-', '_')}": value
            for key, value in (compliance_summary["rohs"] or {}).items()
        }
        analysis.issues.extend(
            compliance_mod.bom_issues(compliance_summary, settings))
        analysis.issues.extend(
            self._bom_issues(results, analysis, settings))

        if self.fx.unknown:
            analysis.warnings.append(
                f"No exchange rate for {', '.join(sorted(self.fx.unknown))}; "
                f"those amounts were left unconverted.")
        elif any(result.cost.indicative_fx for result in results):
            analysis.warnings.append(
                "Some prices were converted using indicative exchange rates, "
                "so totals are approximate.")

        emit("finalise", "Done.", 3, 3)
        analysis.finished_at = _utc_now()
        analysis.duration_ms = int((time.monotonic() - started) * 1000)
        self.db.audit("analysis.complete",
                      f"{bom.name}: {len(results)} lines, health "
                      f"{analysis.health.score}")
        LOG.info("Analysed %s: %d lines in %d ms (health %.1f)",
                 bom.name, len(results), analysis.duration_ms,
                 analysis.health.score)
        return analysis

    # -- stages ----------------------------------------------------------- #

    def _analyse_line(self, line: BomLine,
                      lookups: dict[str, LookupResult],
                      settings: Any) -> LineResult:
        result = LineResult(line=line)

        key = normalize_mpn(line.mpn or line.distributor_pn)
        lookup = lookups.get(key) if key else None
        part: PartData | None = lookup.part if lookup else None
        if lookup:
            result.provider_errors = dict(lookup.errors)

        # match
        result.match = match_mod.classify(
            line, part, fuzzy_threshold=settings.fuzzy_match_threshold)
        if result.match.kind is MatchKind.NONE:
            part = None
        result.part = part
        result.issues.extend(match_mod.match_issues(
            line, result.match, settings.review_confidence_threshold))

        if lookup and lookup.errors and part is None and line.has_part_number:
            detail = "; ".join(f"{k}: {v}" for k, v in
                               list(lookup.errors.items())[:2])
            result.issues.append(Issue(
                code="provider_error",
                message=f"Could not retrieve data for {line.mpn}: {detail}",
                severity=Severity.WARNING, line_no=line.line_no,
                suggestion="Check the provider status in Settings, then "
                           "re-run.",
            ))

        # completions
        if part is not None and settings.auto_complete_fields:
            result.completions = match_mod.propose_completions(
                line, part, result.match)
            if settings.auto_apply_completions and result.completions:
                applied = match_mod.apply_completions(line, result.completions)
                if applied:
                    result.issues.append(Issue(
                        code="fields_completed",
                        message=f"{applied} field(s) were filled in from "
                                f"catalogue data.",
                        severity=Severity.INFO, line_no=line.line_no,
                    ))

        # compliance
        result.compliance = compliance_mod.resolve(part, settings)
        result.issues.extend(compliance_mod.line_issues(result, settings))

        # cost
        result.cost = cost_mod.cost_line(
            part, line.effective_quantity, settings, self.fx)

        return result

    def _add_alternates(self, results: Sequence[LineResult],
                        registry: ProviderRegistry, emit: Callable[..., None],
                        cancel: CancelToken, settings: Any) -> None:
        if settings.max_alternates_per_line <= 0:
            return

        wanted: list[LineResult] = []
        for result in results:
            if result.line.dnp:
                continue
            required = int(result.line.effective_quantity *
                           max(1, settings.build_quantity))
            if result.line.alt_mpns or alternates_mod.needs_alternates(
                    result.part, required, settings):
                wanted.append(result)

        if not wanted:
            emit("alternates", "No lines need alternates.", 1, 1)
            return

        emit("alternates",
             f"Finding alternates for {len(wanted)} line(s)…", 0,
             len(wanted))

        # Resolve BOM-declared alternates in one batch.
        declared_requests: list[tuple[str, str]] = []
        for result in wanted:
            for alt in result.line.alt_mpns:
                declared_requests.append((alt, result.line.manufacturer))
        declared: dict[str, LookupResult] = {}
        if declared_requests:
            try:
                declared = registry.lookup_many(
                    declared_requests,
                    max_workers=settings.max_workers,
                    should_cancel=lambda: cancel.cancelled)
            except Exception as exc:  # pragma: no cover
                LOG.info("Could not resolve declared alternates: %s", exc)

        workers = max(1, min(settings.max_workers, 12))
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._alternates_for, result, registry,
                            declared, settings): result for result in wanted
            }
            for future in as_completed(futures):
                result = futures[future]
                done += 1
                if cancel.cancelled:
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                try:
                    result.alternates = future.result()
                except Exception as exc:  # pragma: no cover
                    LOG.info("Alternates failed for line %s: %s",
                             result.line.line_no, exc)
                if done % 5 == 0 or done == len(wanted):
                    emit("alternates",
                         f"Checked alternates for {done} of {len(wanted)} "
                         f"line(s)", done, len(wanted))

    def _alternates_for(self, result: LineResult, registry: ProviderRegistry,
                        declared: dict[str, LookupResult],
                        settings: Any) -> list:
        line, part = result.line, result.part
        required = int(line.effective_quantity * max(1, settings.build_quantity))
        candidates: list[tuple[PartData, str]] = []

        for alt in line.alt_mpns:
            lookup = declared.get(normalize_mpn(alt))
            if lookup and lookup.part:
                candidates.append((lookup.part, "bom"))

        if part is not None:
            # Manufacturer / distributor published substitutes.
            published = [mpn for mpn in part.alternate_mpns][:6]
            if published:
                try:
                    resolved = registry.lookup_many(
                        [(mpn, "") for mpn in published],
                        max_workers=min(4, settings.max_workers))
                    for lookup in resolved.values():
                        if lookup.part:
                            candidates.append((lookup.part, "substitution"))
                except Exception as exc:  # pragma: no cover
                    LOG.debug("Published alternate lookup failed: %s", exc)

            # Provider-suggested similar parts.
            try:
                for candidate in registry.alternates(
                        part, limit=settings.max_alternates_per_line * 2):
                    candidates.append((candidate, "similar"))
            except Exception as exc:  # pragma: no cover
                LOG.debug("Similar-part lookup failed: %s", exc)

            # Last resort: parametric search.
            if not candidates and (part.lifecycle.is_risky
                                   or not part.in_stock_offers):
                query = alternates_mod.parametric_query(line, part)
                if query:
                    try:
                        for candidate in registry.search(
                                query, limit=settings.max_alternates_per_line * 2):
                            candidates.append((candidate, "search"))
                    except Exception as exc:  # pragma: no cover
                        LOG.debug("Parametric search failed: %s", exc)

        return alternates_mod.build_alternates(
            line, part, candidates, settings, self.fx, required)

    # -- summary ---------------------------------------------------------- #

    def _summarise(self, bom: Bom, results: Sequence[LineResult],
                   registry: ProviderRegistry,
                   settings: Any) -> AnalysisSummary:
        currency = clean(settings.currency).upper() or "USD"
        totals = cost_mod.roll_up([r.cost for r in results],
                                  settings.build_quantity, currency)
        counts = health_mod.counters(results)

        live = [r for r in results if not r.line.dnp]
        lead_times = [
            min((o.lead_time_days for o in r.part.offers
                 if o.lead_time_days is not None), default=None)
            for r in live if r.part
        ]
        lead_times = [days for days in lead_times if days is not None]

        summary = AnalysisSummary(
            total_lines=len(results),
            dnp_lines=sum(1 for r in results if r.line.dnp),
            costed_lines=int(totals["costed_lines"]),
            matched_lines=sum(1 for r in results
                              if r.match.kind is not MatchKind.NONE),
            unmatched_lines=sum(1 for r in live
                                if r.match.kind is MatchKind.NONE
                                and r.line.has_part_number),
            review_lines=sum(1 for r in results if r.match.needs_review),
            placements=bom.placement_count,
            unique_parts=bom.unique_mpns,
            build_quantity=max(1, settings.build_quantity),
            currency=currency,
            total_cost=totals["total_cost"],
            cost_per_unit=totals["cost_per_unit"],
            cost_coverage_pct=float(totals["coverage_pct"]),
            potential_savings=totals["potential_savings"],
            lifecycle_counts=counts["lifecycle"],
            risk_counts=counts["risk"],
            severity_counts=counts["severity"],
            single_source_lines=sum(
                1 for r in live
                if r.part and len({o.distributor for o in r.part.in_stock_offers}) == 1),
            out_of_stock_lines=sum(
                1 for r in live if r.part and not r.part.in_stock_offers),
            long_lead_lines=sum(
                1 for r in live if r.part and not r.part.in_stock_offers
                and any((o.lead_time_days or 0) >= settings.lead_time_warn_days
                        for o in r.part.offers)),
            max_lead_time_days=max(lead_times) if lead_times else None,
            provider_stats=registry.stats(),
            indicative_fx=self.fx.is_indicative(),
            rules={
                "require_rohs": settings.require_rohs,
                "require_reach": settings.require_reach,
                "flag_export_controlled": settings.flag_export_controlled,
                "min_sources_ok": settings.min_sources_ok,
                "lead_time_warn_days": settings.lead_time_warn_days,
                "lead_time_critical_days": settings.lead_time_critical_days,
                "stock_buffer_pct": settings.stock_buffer_pct,
                "review_confidence_threshold":
                    settings.review_confidence_threshold,
                "prefer_authorized_only": settings.prefer_authorized_only,
                "target_region": settings.target_region,
            },
        )
        return summary

    def _bom_issues(self, results: Sequence[LineResult],
                    analysis: BomAnalysis, settings: Any) -> list[Issue]:
        issues: list[Issue] = []
        summary = analysis.summary
        live = [r for r in results if not r.line.dnp]

        if summary.unmatched_lines:
            share = summary.unmatched_lines / max(1, len(live))
            issues.append(Issue(
                code="unmatched_lines",
                message=f"{summary.unmatched_lines} populated line(s) "
                        f"({share * 100:.0f}%) have no catalogue data and are "
                        f"excluded from cost and risk.",
                severity=Severity.ERROR if share > 0.25 else Severity.WARNING,
                suggestion="Check those part numbers for typos, or add a "
                           "provider that carries them.",
            ))
        if summary.review_lines:
            issues.append(Issue(
                code="matches_need_review",
                message=f"{summary.review_lines} line(s) were matched below "
                        f"your confidence threshold and need review.",
                severity=Severity.WARNING,
                suggestion="Open the Review filter to confirm or correct them.",
            ))
        if summary.out_of_stock_lines:
            issues.append(Issue(
                code="out_of_stock",
                message=f"{summary.out_of_stock_lines} line(s) have no stock "
                        f"at any provider queried.",
                severity=Severity.ERROR if summary.out_of_stock_lines > 2
                else Severity.WARNING,
            ))
        if summary.single_source_lines:
            issues.append(Issue(
                code="single_source",
                message=f"{summary.single_source_lines} line(s) are available "
                        f"from only one distributor.",
                severity=Severity.WARNING,
                suggestion="Qualify a second source for these before volume "
                           "production.",
            ))
        if summary.long_lead_lines:
            issues.append(Issue(
                code="long_lead",
                message=f"{summary.long_lead_lines} line(s) are out of stock "
                        f"with lead times at or beyond "
                        f"{settings.lead_time_warn_days} days.",
                severity=Severity.WARNING,
            ))
        if summary.cost_coverage_pct < 90 and summary.total_lines:
            issues.append(Issue(
                code="cost_coverage",
                message=f"Only {summary.cost_coverage_pct:.0f}% of lines "
                        f"needing a price could be costed, so the BOM total is "
                        f"incomplete.",
                severity=Severity.WARNING,
            ))
        return issues

    # -- projects --------------------------------------------------------- #

    def save_project(self, analysis: BomAnalysis, name: str = "",
                     project_id: str | None = None) -> str:
        payload = analysis.to_dict()
        summary = {
            "lines": analysis.summary.total_lines,
            "health": analysis.health.score,
            "grade": analysis.health.grade,
            "total_cost": str(analysis.summary.total_cost or ""),
            "currency": analysis.summary.currency,
            "build_quantity": analysis.summary.build_quantity,
            "offline": analysis.offline,
        }
        return self.projects.save(
            name or analysis.bom.name or "BOM analysis", payload,
            source=analysis.bom.source_file, summary=summary,
            project_id=project_id)

    def load_project(self, project_id: str) -> BomAnalysis | None:
        payload = self.projects.load(project_id)
        if payload is None:
            return None
        return BomAnalysis.from_dict(payload)

    # -- diagnostics ------------------------------------------------------ #

    def status(self) -> dict[str, Any]:
        from .ingest.readers import describe_support

        return {
            "version": __version__,
            "config": self.config.summary(),
            "settings": self.config.settings.to_dict(),
            "providers": self.config.describe_providers(),
            "database": self.db.stats(),
            "cache": {
                "http_hits": self.response_cache.hits,
                "http_misses": self.response_cache.misses,
                "part_hits": self.part_cache.hits,
                "part_misses": self.part_cache.misses,
            },
            "file_support": describe_support(),
            "http": dict(self.http.stats),
            "host_health": self.http.host_health(),
        }

    def test_providers(self) -> list[dict[str, Any]]:
        return self.registry().self_test()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emitter(progress: ProgressFn | None, started: float
             ) -> Callable[..., None]:
    """Build a progress emitter that converts stage progress into an overall %."""
    order = list(STAGE_WEIGHTS)

    def emit(stage: str, message: str, done: int = 0, total: int = 0) -> None:
        if progress is None:
            return
        before = sum(STAGE_WEIGHTS[s] for s in order[:order.index(stage)]) \
            if stage in order else 0.0
        weight = STAGE_WEIGHTS.get(stage, 0.0)
        fraction = (done / total) if total else 0.0
        percent = min(100.0, before + weight * max(0.0, min(1.0, fraction)))
        try:
            progress(Progress(stage=stage, message=message, done=done,
                              total=total, percent=percent,
                              elapsed_ms=int((time.monotonic() - started) * 1000)))
        except Exception:  # pragma: no cover - a UI bug must not stop analysis
            pass

    return emit


def analyse(path: str | Path, config: Config | None = None,
            **options: Any) -> BomAnalysis:
    """Convenience one-liner for scripts and tests."""
    engine = Engine(config)
    try:
        return engine.analyse_file(path, **options)
    finally:
        engine.close()
