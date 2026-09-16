"""
Command line interface.

::

    bomiq analyse BOM.xlsx --build 500 --export xlsx,html
    bomiq inspect BOM.csv                 # show the mapping without analysing
    bomiq part LM358DR                    # single part lookup
    bomiq search "0603 10k 1% resistor"
    bomiq providers --test                # credential check
    bomiq config set currency=EUR build_quantity=1000
    bomiq serve --port 8756               # the web UI and API
    bomiq app                             # the desktop app
    bomiq doctor                          # environment diagnostics

Everything the UI can do is available here, so the engine can be wired into
CI, a nightly obsolescence check, or an ERP import.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import PROVIDER_SPECS, Config
from .core.errors import BomIQError
from .engine import CancelToken, Engine, Progress
from .export import flat, html as html_export, xlsx as xlsx_export
from .ingest.detect import summarise_region
from .util import log
from .util.text import clean
from .version import APP_TITLE

EXPORTERS = {
    "xlsx": (xlsx_export.write_report, ".xlsx"),
    "csv": (flat.write_csv, ".csv"),
    "issues-csv": (flat.write_issues_csv, "-issues.csv"),
    "quote-csv": (flat.write_quote_request_csv, "-quote.csv"),
    "json": (lambda a, p: flat.write_json(a, p, full=True), ".json"),
    "json-flat": (lambda a, p: flat.write_json(a, p, full=False), "-flat.json"),
    "html": (html_export.write_html, ".html"),
}


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bomiq",
        description=f"{APP_TITLE} — local BOM analysis and part "
                    f"intelligence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("::", 1)[-1] if __doc__ else None,
    )
    parser.add_argument("--version", action="version",
                        version=f"bomiq {__version__}")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="suppress progress output")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--data-dir", default=None,
                        help="override the data directory")
    parser.add_argument("--config-dir", default=None,
                        help="override the config directory")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # analyse ------------------------------------------------------------- #
    analyse = subparsers.add_parser(
        "analyse", aliases=["analyze", "run"],
        help="analyse a BOM file end to end")
    analyse.add_argument("file", help="BOM file (csv, xlsx, xls, ods, json, …)")
    analyse.add_argument("-b", "--build", type=int, default=None,
                         metavar="N", help="build quantity")
    analyse.add_argument("-c", "--currency", default=None)
    analyse.add_argument("-p", "--providers", default=None,
                         help="comma separated provider ids to use")
    analyse.add_argument("--offline", action="store_true",
                         help="use only the built-in offline catalogue")
    analyse.add_argument("-e", "--export", default=None,
                         help=f"comma separated formats: "
                              f"{', '.join(sorted(EXPORTERS))}")
    analyse.add_argument("-o", "--out", default=None,
                         help="output directory or file stem")
    analyse.add_argument("--sheet", default=None, help="force a sheet name")
    analyse.add_argument("--header-row", type=int, default=None,
                         help="force the header row (1-based)")
    analyse.add_argument("--no-merge", action="store_true",
                         help="do not merge duplicate part numbers")
    analyse.add_argument("--save", metavar="NAME", default=None,
                         help="save the analysis as a project")
    analyse.add_argument("--fail-on", default=None,
                         choices=["error", "warning", "critical", "high"],
                         help="exit non-zero when findings at this level exist "
                              "(for CI)")
    analyse.add_argument("--min-health", type=float, default=None,
                         help="exit non-zero if BOM health is below this")
    analyse.add_argument("--json", action="store_true",
                         help="print the full analysis as JSON on stdout")
    analyse.add_argument("--limit-issues", type=int, default=25)

    # inspect -------------------------------------------------------------- #
    inspect = subparsers.add_parser(
        "inspect", help="parse a BOM and show the column mapping only")
    inspect.add_argument("file")
    inspect.add_argument("--sheet", default=None)
    inspect.add_argument("--header-row", type=int, default=None)
    inspect.add_argument("--rows", type=int, default=8,
                         help="preview rows to print")
    inspect.add_argument("--json", action="store_true")

    # part ----------------------------------------------------------------- #
    part = subparsers.add_parser("part", help="look up one part number")
    part.add_argument("mpn")
    part.add_argument("-m", "--manufacturer", default="")
    part.add_argument("-p", "--providers", default=None)
    part.add_argument("--json", action="store_true")

    # search --------------------------------------------------------------- #
    search = subparsers.add_parser("search", help="keyword part search")
    search.add_argument("query", nargs="+")
    search.add_argument("-n", "--limit", type=int, default=10)
    search.add_argument("--json", action="store_true")

    # providers ------------------------------------------------------------ #
    providers = subparsers.add_parser("providers",
                                      help="list or test data providers")
    providers.add_argument("--test", action="store_true",
                           help="make a live call to each enabled provider")
    providers.add_argument("--set", nargs="+", metavar="ID.KEY=VALUE",
                           default=None,
                           help="store a credential, e.g. "
                                "mouser.api_key=abc123")
    providers.add_argument("--enable", default=None,
                           help="comma separated provider ids to enable")
    providers.add_argument("--json", action="store_true")

    # config --------------------------------------------------------------- #
    config_parser = subparsers.add_parser("config",
                                          help="read or write settings")
    config_parser.add_argument("action", choices=["show", "set", "path"],
                               nargs="?", default="show")
    config_parser.add_argument("pairs", nargs="*", metavar="KEY=VALUE")

    # projects ------------------------------------------------------------- #
    projects = subparsers.add_parser("projects", help="saved analyses")
    projects.add_argument("action", choices=["list", "show", "delete",
                                             "export"], nargs="?",
                          default="list")
    projects.add_argument("project_id", nargs="?")
    projects.add_argument("-e", "--export", default="xlsx")
    projects.add_argument("-o", "--out", default=None)

    # serve ---------------------------------------------------------------- #
    serve_parser = subparsers.add_parser("serve",
                                         help="run the local web UI and API")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=0)
    serve_parser.add_argument("--open", action="store_true",
                              help="open a browser window")
    serve_parser.add_argument("--no-token", action="store_true",
                              help="disable the session token (only do this on "
                                   "a machine you trust completely)")

    # app ------------------------------------------------------------------ #
    app_parser = subparsers.add_parser("app", help="run the desktop app")
    app_parser.add_argument("--port", type=int, default=0)
    app_parser.add_argument("file", nargs="?", help="BOM to open on start")

    # doctor --------------------------------------------------------------- #
    doctor = subparsers.add_parser("doctor",
                                   help="environment and install diagnostics")
    doctor.add_argument("--json", action="store_true")

    # cache ---------------------------------------------------------------- #
    cache = subparsers.add_parser("cache", help="manage the local cache")
    cache.add_argument("action", choices=["stats", "prune", "clear"],
                       nargs="?", default="stats")

    return parser


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def make_engine(args: argparse.Namespace) -> Engine:
    config = Config(data_dir=args.data_dir, config_dir=args.config_dir)
    if getattr(args, "offline", False):
        config.settings.offline = True
    if getattr(args, "build", None):
        config.settings.build_quantity = int(args.build)
    if getattr(args, "currency", None):
        config.settings.currency = clean(args.currency).upper()
    if args.log_level:
        config.settings.log_level = args.log_level
    log.setup(config.log_dir, config.settings.log_level,
              console=not args.quiet)
    return Engine(config)


def cmd_analyse(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        provider_ids = [p.strip() for p in args.providers.split(",")] \
            if args.providers else None

        printer = _progress_printer(args.quiet)
        options: dict[str, Any] = {}
        if args.sheet:
            options["sheet_name"] = args.sheet
        if args.header_row:
            options["header_row"] = args.header_row
        if args.no_merge:
            options["merge_duplicate_mpns"] = False

        analysis = engine.analyse_file(
            args.file, progress=printer, cancel=CancelToken(),
            provider_ids=provider_ids, **options)

        if args.json:
            print(json.dumps(analysis.to_dict(), default=str, indent=2))
        else:
            if not args.quiet:
                print()
            print(flat.summary_text(analysis))
            issues = flat.issue_lines(analysis, "warning",
                                      limit=args.limit_issues)
            if issues:
                print(f"\nFindings (warning and above, "
                      f"first {len(issues)}):")
                print("\n".join(issues))
            top = [r for r in analysis.results
                   if r.risk.score >= 50 and not r.line.dnp]
            if top:
                print(f"\nLines to look at first:")
                rows = [
                    [r.line.line_no, r.line.mpn,
                     r.part.lifecycle.value if r.part else "no data",
                     f"{r.risk.score:.0f}", r.risk.level.value,
                     ", ".join(r.risk.flags),
                     r.alternates[0].mpn if r.alternates else ""]
                    for r in sorted(top, key=lambda r: -r.risk.score)[:15]
                ]
                print(flat.render_table(
                    rows, ["Line", "MPN", "Lifecycle", "Risk", "Level",
                           "Flags", "Best alternate"]))

        if args.export:
            written = _export(analysis, args.export, args.out, args.file)
            if not args.quiet:
                print("\nWritten:")
                for path in written:
                    print(f"  {path}")

        if args.save:
            project_id = engine.save_project(analysis, name=args.save)
            print(f"\nSaved as project {project_id}")

        return _exit_code(analysis, args)
    finally:
        engine.close()


def cmd_inspect(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        options: dict[str, Any] = {}
        if args.sheet:
            options["sheet_name"] = args.sheet
        if args.header_row:
            options["header_row"] = args.header_row
        result = engine.ingest(args.file, **options)
        if args.json:
            print(json.dumps(result.to_dict(), default=str, indent=2))
            return 0

        mapping = result.mapping
        print(f"File            {args.file}")
        print(f"Format          {result.bom.source_format}")
        print(f"Sheets read     {', '.join(result.bom.sheets_read)}")
        if result.chosen:
            summary = summarise_region(result.chosen)
            print(f"Header row      {summary['header_row']} "
                  f"(first column {summary['first_column']})")
        print(f"Lines           {result.bom.line_count}")
        print(f"Confidence      {mapping.overall_confidence:.0f}%"
              f"{f'  (template: {mapping.template_name})' if mapping.template_name else ''}")
        if result.bom.metadata:
            print("Metadata        " + "; ".join(
                f"{k}={v}" for k, v in result.bom.metadata.items()))
        print("\nColumn mapping:")
        rows = [
            [field, mapping.headers[index] if index < len(mapping.headers)
             else "?", f"{mapping.confidence.get(field, 0):.0f}%",
             mapping.reasons.get(field, "")]
            for field, index in sorted(mapping.mapping.items())
        ]
        print(flat.render_table(rows, ["Field", "Column", "Confidence",
                                       "Why"]))
        if mapping.unmapped_indices:
            print("\nUnmapped columns: " + ", ".join(
                mapping.headers[i] or f"(column {i + 1})"
                for i in mapping.unmapped_indices))

        print("\nFirst lines:")
        rows = [
            [line.line_no, line.mpn, line.manufacturer,
             f"{line.quantity:g}", line.ref_text,
             "DNP" if line.dnp else "", line.description]
            for line in result.bom.lines[:args.rows]
        ]
        print(flat.render_table(rows, ["#", "MPN", "Manufacturer", "Qty",
                                       "Refs", "", "Description"]))

        findings = [i for i in result.bom.issues if i.severity.value != "info"]
        if findings:
            print("\nFindings:")
            for issue in findings[:20]:
                print(f"  [{issue.severity.value.upper():7s}] {issue.message}")
        return 0
    finally:
        engine.close()


def cmd_part(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        provider_ids = [p.strip() for p in args.providers.split(",")] \
            if args.providers else None
        registry = engine.registry(provider_ids)
        result = registry.lookup(args.mpn, args.manufacturer)
        if args.json:
            print(json.dumps(result.to_dict(), default=str, indent=2))
            return 0 if result.found else 1
        if not result.found:
            print(f"Not found: {args.mpn}")
            for provider, message in result.errors.items():
                print(f"  {provider}: {message}")
            return 1
        part = result.part
        print(f"{part.mpn}   {part.manufacturer}")
        print(f"  {part.description}")
        print(f"  Category      {part.category}")
        print(f"  Package       {part.package} {part.mount}")
        print(f"  Lifecycle     {part.lifecycle.value}  {part.lifecycle_note}")
        print(f"  Stock         {part.total_stock:,} across "
              f"{part.distributor_count} distributor(s)")
        if part.estimated_factory_lead_days:
            print(f"  Factory lead  {part.estimated_factory_lead_days} days")
        compliance = part.compliance
        print(f"  RoHS          {compliance.rohs.value} {compliance.rohs_note}")
        print(f"  REACH         {compliance.reach.value}")
        print(f"  Origin / HTS  {compliance.country_of_origin} / "
              f"{compliance.hts_code}")
        if part.datasheet_url:
            print(f"  Datasheet     {part.datasheet_url}")
        if part.offers:
            print("\nOffers:")
            rows = [
                [offer.distributor, offer.sku, offer.stock or 0,
                 offer.moq or "", offer.spq or "",
                 f"{offer.unit_price_at_one or ''}",
                 f"{offer.min_unit_price or ''}", offer.currency,
                 offer.lead_time_days if offer.lead_time_days is not None else ""]
                for offer in sorted(part.offers,
                                    key=lambda o: -(o.stock or 0))
            ]
            print(flat.render_table(rows, ["Distributor", "SKU", "Stock",
                                           "MOQ", "Pack", "Price@1",
                                           "Best price", "Cur", "Lead"]))
        if part.alternate_mpns or part.similar_mpns:
            print("\nAlternates: " + ", ".join(
                (part.alternate_mpns + part.similar_mpns)[:10]))
        for provider, message in result.errors.items():
            print(f"\nNote: {provider} — {message}")
        return 0
    finally:
        engine.close()


def cmd_search(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        query = " ".join(args.query)
        parts = engine.registry().search(query, limit=args.limit)
        if args.json:
            print(json.dumps([p.to_dict() for p in parts], default=str,
                             indent=2))
            return 0
        if not parts:
            print(f"Nothing found for {query!r}")
            return 1
        rows = [
            [p.mpn, p.manufacturer, p.lifecycle.value, p.total_stock,
             p.median_price_1k or "", p.description]
            for p in parts
        ]
        print(flat.render_table(rows, ["MPN", "Manufacturer", "Lifecycle",
                                       "Stock", "~1k price", "Description"]))
        return 0
    finally:
        engine.close()


def cmd_providers(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        if args.set:
            for pair in args.set:
                if "=" not in pair or "." not in pair.split("=")[0]:
                    print(f"Skipping {pair!r}: expected ID.KEY=VALUE")
                    continue
                target, value = pair.split("=", 1)
                provider_id, key = target.split(".", 1)
                if provider_id not in PROVIDER_SPECS:
                    print(f"Unknown provider {provider_id!r}")
                    continue
                engine.config.credentials.set(provider_id, {key: value.strip()})
                print(f"Stored {provider_id}.{key}")
            engine.config.save()

        if args.enable:
            wanted = [p.strip() for p in args.enable.split(",") if p.strip()]
            unknown = [p for p in wanted if p not in PROVIDER_SPECS]
            if unknown:
                print(f"Unknown provider(s): {', '.join(unknown)}")
                return 2
            engine.config.settings.providers = wanted
            engine.config.save()
            print(f"Enabled: {', '.join(wanted)}")

        described = engine.config.describe_providers()
        if args.test:
            results = engine.test_providers()
            by_id = {r["provider"]: r for r in results}
            for provider in described:
                provider["test"] = by_id.get(provider["id"], {})

        if args.json:
            print(json.dumps(described, default=str, indent=2))
            return 0

        rows = []
        for provider in described:
            test = provider.get("test") or {}
            rows.append([
                provider["id"], provider["name"], provider["kind"],
                "yes" if provider["selected"] else "",
                "yes" if provider["configured"] else "",
                "yes" if provider["active"] else "",
                ("ok" if test.get("ok") else "fail") if test else "",
                test.get("message", "") if test else provider["notes"][:60],
            ])
        print(flat.render_table(rows, ["id", "Name", "Kind", "Selected",
                                       "Key", "Active", "Test", "Notes"]))
        if not any(p["active"] and p["id"] != "mock" for p in described):
            print("\nNo live provider is configured, so the offline catalogue "
                  "will be used.\nAdd a key with, for example:\n"
                  "  bomiq providers --set mouser.api_key=YOUR_KEY "
                  "--enable mouser")
        return 0
    finally:
        engine.close()


def cmd_config(args: argparse.Namespace) -> int:
    config = Config(data_dir=args.data_dir, config_dir=args.config_dir)
    if args.action == "path":
        print(config.settings_file)
        return 0
    if args.action == "set":
        if not args.pairs:
            print("Nothing to set. Use KEY=VALUE pairs.")
            return 2
        updates: dict[str, Any] = {}
        for pair in args.pairs:
            if "=" not in pair:
                print(f"Skipping {pair!r}: expected KEY=VALUE")
                continue
            key, value = pair.split("=", 1)
            updates[key.strip()] = value.strip()
        changed = config.settings.update(updates)
        config.save()
        unknown = [k for k in updates if k not in changed]
        print(f"Changed: {', '.join(changed) or '(nothing)'}")
        if unknown:
            print(f"Unchanged or unknown: {', '.join(unknown)}")
        return 0
    print(json.dumps(config.settings.to_dict(), indent=2, default=str))
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        if args.action == "list":
            projects = engine.projects.list()
            if not projects:
                print("No saved projects.")
                return 0
            rows = [
                [p["id"], p["name"], (p["summary"] or {}).get("lines", ""),
                 (p["summary"] or {}).get("health", ""),
                 (p["summary"] or {}).get("total_cost", ""),
                 (p["summary"] or {}).get("currency", "")]
                for p in projects
            ]
            print(flat.render_table(rows, ["id", "Name", "Lines", "Health",
                                           "Cost", "Cur"]))
            return 0
        if not args.project_id:
            print("A project id is required.")
            return 2
        analysis = engine.load_project(args.project_id)
        if analysis is None:
            print(f"No project {args.project_id!r}")
            return 1
        if args.action == "show":
            print(flat.summary_text(analysis))
            return 0
        if args.action == "delete":
            engine.projects.delete(args.project_id)
            print("Deleted.")
            return 0
        written = _export(analysis, args.export, args.out,
                          analysis.bom.name or "project")
        for path in written:
            print(path)
        return 0
    finally:
        engine.close()


def cmd_serve(args: argparse.Namespace) -> int:
    from .server.app import serve

    config = Config(data_dir=args.data_dir, config_dir=args.config_dir)
    log.setup(config.log_dir, config.settings.log_level, console=True)
    serve(config, host=args.host, port=args.port, open_browser=args.open,
          require_token=not args.no_token)
    return 0


def cmd_app(args: argparse.Namespace) -> int:
    from .desktop import main as desktop_main

    return desktop_main(port=args.port, open_file=args.file,
                        data_dir=args.data_dir, config_dir=args.config_dir)


def cmd_doctor(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        status = engine.status()
        checks = _doctor_checks(engine, status)
        if args.json:
            print(json.dumps({"status": status, "checks": checks},
                             default=str, indent=2))
            return 0 if all(c["ok"] for c in checks) else 1
        print(f"{APP_TITLE}  v{__version__}\n")
        for check in checks:
            mark = "ok  " if check["ok"] else "WARN"
            print(f"  [{mark}] {check['name']:28s} {check['detail']}")
        print("\nPaths:")
        for key in ("data_dir", "config_dir", "db_path"):
            print(f"  {key:12s} {status['config'][key]}")
        print("\nFile support:")
        support = status["file_support"]
        print(f"  extensions   {' '.join(support['extensions'])}")
        print(f"  xlsx engine  {support['xlsx_engine']}")
        print(f"  legacy .xls  {'yes' if support['legacy_xls'] else 'no'}")
        return 0 if all(c["ok"] for c in checks) else 1
    finally:
        engine.close()


def cmd_cache(args: argparse.Namespace) -> int:
    engine = make_engine(args)
    try:
        if args.action == "prune":
            removed = engine.db.prune()
            print("Removed: " + ", ".join(f"{k}={v}" for k, v in
                                          removed.items()))
        elif args.action == "clear":
            engine.db.clear_caches()
            print("Cache cleared.")
        stats = engine.db.stats()
        print(json.dumps(stats, indent=2, default=str))
        return 0
    finally:
        engine.close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _progress_printer(quiet: bool):
    if quiet:
        return None
    state = {"last": ""}

    def report(event: Progress) -> None:
        line = f"  [{event.percent:5.1f}%] {event.stage:10s} {event.message}"
        if line != state["last"]:
            sys.stderr.write(line[:160] + "\n")
            sys.stderr.flush()
            state["last"] = line

    return report


def _export(analysis: Any, formats: str, out: str | None,
            source: str) -> list[Path]:
    wanted = [f.strip() for f in formats.split(",") if f.strip()]
    unknown = [f for f in wanted if f not in EXPORTERS]
    if unknown:
        raise BomIQError(
            f"Unknown export format(s): {', '.join(unknown)}. "
            f"Available: {', '.join(sorted(EXPORTERS))}")

    if out:
        out_path = Path(out).expanduser()
        # A path with no file extension is a directory: that is what someone
        # means by "-o reports", and creating "reports.xlsx" instead would be
        # a surprise.
        looks_like_dir = (out_path.is_dir() or out.endswith(("/", "\\"))
                          or not out_path.suffix)
        if looks_like_dir:
            directory, stem = out_path, Path(source).stem
        else:
            directory, stem = out_path.parent, out_path.stem
    else:
        directory, stem = Path.cwd(), Path(source).stem
    directory.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for fmt in wanted:
        writer, suffix = EXPORTERS[fmt]
        written.append(Path(writer(analysis, directory / f"{stem}{suffix}")))
    return written


def _exit_code(analysis: Any, args: argparse.Namespace) -> int:
    if args.min_health is not None and analysis.health.score < args.min_health:
        print(f"\nFAIL: health {analysis.health.score:.1f} is below the "
              f"required {args.min_health:.1f}", file=sys.stderr)
        return 3
    if not args.fail_on:
        return 0
    level = args.fail_on
    if level in ("critical", "high"):
        wanted = {"critical": ("Critical",),
                  "high": ("Critical", "High")}[level]
        offenders = [r for r in analysis.results
                     if r.risk.level.value in wanted and not r.line.dnp]
        if offenders:
            print(f"\nFAIL: {len(offenders)} line(s) at {level} risk or above",
                  file=sys.stderr)
            return 4
        return 0
    ranks = {"warning": 1, "error": 2}
    threshold = ranks[level]
    count = sum(1 for issue in analysis.issues
                if issue.severity.rank >= threshold)
    count += sum(1 for result in analysis.results
                 for issue in result.all_issues
                 if issue.severity.rank >= threshold)
    if count:
        print(f"\nFAIL: {count} finding(s) at {level} or above",
              file=sys.stderr)
        return 5
    return 0


def _doctor_checks(engine: Engine, status: dict[str, Any]
                   ) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    version = sys.version_info
    add("Python version", version >= (3, 9),
        f"{version.major}.{version.minor}.{version.micro}"
        f"{'' if version >= (3, 9) else ' (3.9 or newer is required)'}")

    data_dir = Path(status["config"]["data_dir"])
    writable = False
    try:
        probe = data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
    except OSError as exc:
        add("Data directory writable", False, f"{data_dir}: {exc}")
    if writable:
        add("Data directory writable", True, str(data_dir))

    db = status["database"]
    add("Local database", True,
        f"{db['size_bytes'] / 1024:.0f} kB, {db['part_cache']} cached parts, "
        f"{db['templates']} templates")

    support = status["file_support"]
    add("Spreadsheet reader", True,
        f"{support['xlsx_engine']} for .xlsx; legacy .xls "
        f"{'available' if support['legacy_xls'] else 'needs the optional xlrd package'}")

    live = [p for p in status["providers"]
            if p["active"] and p["id"] != "mock"]
    add("Live data providers", bool(live),
        ", ".join(p["name"] for p in live) if live
        else "none configured — the offline catalogue will be used")

    unconfigured = [p["name"] for p in status["providers"]
                    if p["selected"] and not p["configured"]
                    and p["id"] != "mock"]
    if unconfigured:
        add("Selected but no key", False, ", ".join(unconfigured))

    add("Credential storage", True,
        "OS keyring" if status["config"]["keyring"]
        else "file with owner-only permissions (install 'keyring' to use the "
             "OS store)")

    try:
        import ssl

        add("TLS support", True, ssl.OPENSSL_VERSION)
    except Exception as exc:  # pragma: no cover
        add("TLS support", False, str(exc))

    return checks


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

COMMANDS = {
    "analyse": cmd_analyse, "analyze": cmd_analyse, "run": cmd_analyse,
    "inspect": cmd_inspect, "part": cmd_part, "search": cmd_search,
    "providers": cmd_providers, "config": cmd_config, "projects": cmd_projects,
    "serve": cmd_serve, "app": cmd_app, "doctor": cmd_doctor,
    "cache": cmd_cache,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse prevents this
        parser.error(f"Unknown command {args.command!r}")
    try:
        return handler(args)
    except BomIQError as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover - piping into head
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
