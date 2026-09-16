"""Tests for matching, risk, cost, alternates, compliance and health."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bomiq.analysis import alternates as alt_mod  # noqa: E402
from bomiq.analysis import compliance as comp_mod  # noqa: E402
from bomiq.analysis import cost as cost_mod  # noqa: E402
from bomiq.analysis import health as health_mod  # noqa: E402
from bomiq.analysis import match as match_mod  # noqa: E402
from bomiq.analysis import risk as risk_mod  # noqa: E402
from bomiq.config import Settings  # noqa: E402
from bomiq.core.models import (  # noqa: E402
    BomLine, Compliance, ComplianceState, Lifecycle, LineResult, Match,
    MatchKind, Offer, PartData, PriceBreak, RiskLevel,
)
from bomiq.providers.base import (  # noqa: E402
    build_price_breaks, merge_parts, normalize_compliance_state,
    normalize_lifecycle,
)
from bomiq.util.money import FxTable  # noqa: E402


def make_offer(distributor="DigiKey", stock=10_000, moq=1, spq=1,
               price="1.00", currency="USD", lead=0, authorized=True,
               breaks=None):
    ladder = breaks or [(1, price), (100, str(float(price) * 0.8)),
                        (1000, str(float(price) * 0.6))]
    return Offer(
        provider="test", distributor=distributor, sku=f"{distributor}-1",
        stock=stock, moq=moq, spq=spq, lead_time_days=lead,
        currency=currency, authorized=authorized,
        price_breaks=[PriceBreak(quantity=q, unit_price=Decimal(str(p)),
                                 currency=currency) for q, p in ladder],
    )


def make_part(mpn="LM358DR", manufacturer="Texas Instruments",
              lifecycle=Lifecycle.ACTIVE, offers=None, package="SOIC-8",
              description="IC OPAMP GP 2 CIRCUIT SOIC-8", specs=None,
              compliance=None):
    return PartData(
        mpn=mpn, manufacturer=manufacturer, description=description,
        package=package, lifecycle=lifecycle,
        offers=offers if offers is not None else [make_offer()],
        specs=specs or {}, providers=["test"],
        compliance=compliance or Compliance(
            rohs=ComplianceState.COMPLIANT,
            reach=ComplianceState.COMPLIANT),
    )


def make_line(**kwargs):
    defaults = dict(line_no=1, mpn="LM358DR",
                    manufacturer="Texas Instruments", quantity=1.0,
                    package="SOIC-8")
    defaults.update(kwargs)
    return BomLine(**defaults)


# --------------------------------------------------------------------------- #
# Provider vocabulary
# --------------------------------------------------------------------------- #

class TestVocabulary(unittest.TestCase):
    def test_lifecycle_synonyms(self):
        cases = {
            "Active": Lifecycle.ACTIVE,
            "Production": Lifecycle.ACTIVE,
            "Not Recommended for New Designs": Lifecycle.NRND,
            "NRND": Lifecycle.NRND,
            "Last Time Buy": Lifecycle.EOL,
            "End of Life": Lifecycle.EOL,
            "Obsolete": Lifecycle.OBSOLETE,
            "Discontinued at Digi-Key": Lifecycle.OBSOLETE,
            "New Product": Lifecycle.NEW,
            "": Lifecycle.UNKNOWN,
        }
        for text, expected in cases.items():
            self.assertEqual(normalize_lifecycle(text)[0], expected, text)

    def test_worst_lifecycle_wins(self):
        state, _ = normalize_lifecycle("Active", "Obsolete", "NRND")
        self.assertEqual(state, Lifecycle.OBSOLETE)

    def test_compliance_states(self):
        self.assertEqual(normalize_compliance_state("ROHS3 Compliant"),
                         ComplianceState.COMPLIANT)
        self.assertEqual(normalize_compliance_state("Non-Compliant"),
                         ComplianceState.NON_COMPLIANT)
        self.assertEqual(normalize_compliance_state("RoHS by Exemption"),
                         ComplianceState.EXEMPT)
        self.assertEqual(normalize_compliance_state("unknown"),
                         ComplianceState.UNKNOWN)

    def test_rohs_non_compliant_is_not_read_as_compliant(self):
        self.assertEqual(normalize_compliance_state("RoHS non-compliant"),
                         ComplianceState.NON_COMPLIANT)

    def test_price_break_normalisation(self):
        breaks = build_price_breaks([
            {"BreakQuantity": 1, "UnitPrice": 1.5},
            {"BreakQuantity": 10, "UnitPrice": 1.2},
            {"BreakQuantity": 10, "UnitPrice": 1.1},     # duplicate, cheaper
        ])
        self.assertEqual(len(breaks), 2)
        self.assertEqual(breaks[1].unit_price, Decimal("1.1"))

    def test_merge_parts_takes_worst_lifecycle_and_all_offers(self):
        a = make_part(lifecycle=Lifecycle.ACTIVE,
                      offers=[make_offer("DigiKey")])
        a.providers = ["digikey"]
        b = make_part(lifecycle=Lifecycle.NRND,
                      offers=[make_offer("Mouser")])
        b.providers = ["mouser"]
        merged = merge_parts([a, b])
        self.assertEqual(merged.lifecycle, Lifecycle.NRND)
        self.assertEqual(len(merged.offers), 2)
        self.assertEqual(sorted(merged.providers), ["digikey", "mouser"])


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #

class TestMatching(unittest.TestCase):
    def test_exact_with_manufacturer(self):
        result = match_mod.classify(make_line(), make_part())
        self.assertEqual(result.kind, MatchKind.EXACT_WITH_MFR)
        self.assertEqual(result.confidence, 100)

    def test_manufacturer_conflict_lowers_confidence(self):
        line = make_line(manufacturer="Onsemi")
        result = match_mod.classify(line, make_part())
        self.assertLess(result.confidence, 80)
        self.assertTrue(any("Manufacturer differs" in reason
                            for reason in result.reasons))

    def test_packaging_suffix_match(self):
        line = make_line(mpn="GRM188R71C104KA01D-TR", package="0603")
        part = make_part(mpn="GRM188R71C104KA01D",
                         manufacturer="Murata Electronics", package="0603",
                         description="CAP CER 0.1UF 16V X7R 0603")
        result = match_mod.classify(line, part)
        self.assertEqual(result.kind, MatchKind.ROOT)

    def test_no_match_when_too_different(self):
        line = make_line(mpn="TOTALLY-DIFFERENT-9999")
        result = match_mod.classify(line, make_part())
        self.assertEqual(result.kind, MatchKind.NONE)
        self.assertEqual(result.confidence, 0)

    def test_none_part_is_no_match(self):
        result = match_mod.classify(make_line(), None)
        self.assertEqual(result.kind, MatchKind.NONE)

    def test_package_disagreement_penalised(self):
        line = make_line(package="DIP-8")
        result = match_mod.classify(line, make_part(package="SOIC-8"))
        self.assertLess(result.confidence, 100)

    def test_synthesised_record_caps_confidence(self):
        part = make_part(specs={"Record type": "Synthesised — placeholder"})
        result = match_mod.classify(make_line(), part)
        self.assertLessEqual(result.confidence, 40)

    def test_low_confidence_raises_a_review_issue(self):
        line = make_line(manufacturer="Onsemi")
        result = match_mod.classify(line, make_part())
        issues = match_mod.match_issues(line, result, review_threshold=90)
        self.assertTrue(any(i.code == "match_needs_review" for i in issues))

    def test_unmatched_line_with_part_number_is_an_error(self):
        line = make_line()
        issues = match_mod.match_issues(line, Match(kind=MatchKind.NONE))
        self.assertTrue(any(i.code == "no_catalogue_match" for i in issues))

    def test_dnp_line_is_not_flagged_for_no_match(self):
        line = make_line(dnp=True)
        issues = match_mod.match_issues(line, Match(kind=MatchKind.NONE))
        self.assertEqual(issues, [])


class TestCompletions(unittest.TestCase):
    def test_fills_blank_fields(self):
        line = make_line(manufacturer="", package="")
        part = make_part()
        match = match_mod.classify(line, part)
        completions = match_mod.propose_completions(line, part, match)
        fields = {c.field for c in completions}
        self.assertIn("manufacturer", fields)
        self.assertIn("package", fields)

    def test_nothing_proposed_for_low_confidence(self):
        line = make_line(manufacturer="", mpn="SOMETHING-ELSE")
        part = make_part()
        match = Match(kind=MatchKind.FUZZY, confidence=40)
        self.assertEqual(match_mod.propose_completions(line, part, match), [])

    def test_apply_writes_values(self):
        line = make_line(manufacturer="")
        part = make_part()
        match = match_mod.classify(line, part)
        completions = match_mod.propose_completions(line, part, match)
        applied = match_mod.apply_completions(line, completions)
        self.assertGreaterEqual(applied, 1)
        self.assertEqual(line.manufacturer, "Texas Instruments")


# --------------------------------------------------------------------------- #
# Costing
# --------------------------------------------------------------------------- #

class TestCosting(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.build_quantity = 100
        self.settings.currency = "USD"
        self.fx = FxTable()

    def test_simple_cost(self):
        part = make_part(offers=[make_offer(price="1.00")])
        cost = cost_mod.cost_line(part, 2.0, self.settings, self.fx)
        self.assertEqual(cost.required_qty, 200)
        # 200 pieces sits on the 100-piece break at 0.80
        self.assertEqual(cost.unit_price, Decimal("0.800000"))
        self.assertEqual(cost.extended, Decimal("160.00"))

    def test_moq_forces_an_overbuy_and_says_so(self):
        part = make_part(offers=[make_offer(moq=4000, spq=4000, stock=10_000)])
        cost = cost_mod.cost_line(part, 1.0, self.settings, self.fx)
        self.assertEqual(cost.best.order_qty, 4000)
        self.assertEqual(cost.best.overbuy_qty, 3900)
        self.assertTrue(any("Minimum order" in note
                            for note in cost.best.notes))

    def test_cheapest_total_wins(self):
        part = make_part(offers=[
            make_offer("Expensive", price="2.00"),
            make_offer("Cheap", price="1.00"),
        ])
        cost = cost_mod.cost_line(part, 1.0, self.settings, self.fx)
        self.assertEqual(cost.best.distributor, "Cheap")

    def test_savings_are_computed_per_piece_not_on_extended_totals(self):
        """Regression: a big reel must not look like a huge saving."""
        part = make_part(offers=[
            make_offer("Cut tape", price="2.00", moq=1, spq=1, stock=10_000),
            make_offer("Reel", price="3.00", moq=4000, spq=4000, stock=10_000),
        ])
        cost = cost_mod.cost_line(part, 1.0, self.settings, self.fx)
        # 100 needed: cut tape 2.00 -> 1.60 at the 100 break; reel 3.00 -> 1.80.
        # The saving must be a per-piece difference times 100, not the
        # difference between 160 and 7200.
        self.assertIsNotNone(cost.savings_vs_worst)
        self.assertLess(cost.savings_vs_worst, Decimal("100"))

    def test_dnp_line_is_not_costed(self):
        cost = cost_mod.cost_line(make_part(), 0.0, self.settings, self.fx)
        self.assertEqual(cost.extended, None)
        self.assertTrue(cost.notes)

    def test_no_part_is_not_costed(self):
        cost = cost_mod.cost_line(None, 1.0, self.settings, self.fx)
        self.assertIsNone(cost.extended)

    def test_median_price_fallback_is_marked_estimated(self):
        part = make_part(offers=[])
        part.median_price_1k = Decimal("0.50")
        cost = cost_mod.cost_line(part, 1.0, self.settings, self.fx)
        self.assertTrue(cost.estimated)
        self.assertEqual(cost.extended, Decimal("50.00"))

    def test_currency_conversion_applied(self):
        self.settings.currency = "EUR"
        part = make_part(offers=[make_offer(price="1.00", currency="USD")])
        cost = cost_mod.cost_line(part, 1.0, self.settings, self.fx)
        self.assertLess(cost.unit_price, Decimal("1.00"))
        self.assertTrue(cost.indicative_fx)

    def test_split_order_when_one_source_is_short(self):
        part = make_part(offers=[
            make_offer("A", stock=60, price="1.00"),
            make_offer("B", stock=60, price="1.10"),
        ])
        cost = cost_mod.cost_line(part, 1.0, self.settings, self.fx)
        self.assertTrue(any("split" in note.lower() for note in cost.notes)
                        or cost.best is not None)

    def test_price_curve_has_a_point_per_decade(self):
        part = make_part(offers=[make_offer()])
        cost = cost_mod.cost_line(part, 1.0, self.settings, self.fx)
        self.assertGreaterEqual(len(cost.price_curve), 4)
        quantities = [point["build_qty"] for point in cost.price_curve]
        self.assertEqual(quantities, sorted(quantities))

    def test_roll_up(self):
        part = make_part(offers=[make_offer(price="1.00")])
        costs = [cost_mod.cost_line(part, 1.0, self.settings, self.fx)
                 for _ in range(3)]
        totals = cost_mod.roll_up(costs, 100, "USD")
        self.assertEqual(totals["costed_lines"], 3)
        self.assertEqual(totals["coverage_pct"], 100.0)
        self.assertEqual(totals["total_cost"], Decimal("240.00"))


# --------------------------------------------------------------------------- #
# Risk
# --------------------------------------------------------------------------- #

class TestRisk(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.settings.build_quantity = 100
        self.fx = FxTable()

    def score(self, part, line=None):
        line = line or make_line()
        result = LineResult(line=line, part=part)
        result.match = match_mod.classify(line, part)
        result.compliance = comp_mod.resolve(part, self.settings)
        result.cost = cost_mod.cost_line(part, line.effective_quantity,
                                         self.settings, self.fx)
        result.risk = risk_mod.score_line(result, self.settings)
        return result

    def test_healthy_part_is_low_risk(self):
        part = make_part(offers=[make_offer("A"), make_offer("B"),
                                 make_offer("C")])
        result = self.score(part)
        self.assertEqual(result.risk.level, RiskLevel.LOW)

    def test_obsolete_part_is_high_or_critical(self):
        part = make_part(lifecycle=Lifecycle.OBSOLETE, offers=[])
        result = self.score(part)
        self.assertIn(result.risk.level, (RiskLevel.HIGH, RiskLevel.CRITICAL))
        self.assertIn("Obsolete", result.risk.flags)

    def test_eol_is_never_low_even_when_cheap_and_stocked(self):
        """Regression: obsolescence must not be averaged away."""
        part = make_part(lifecycle=Lifecycle.EOL,
                         offers=[make_offer("A", stock=1_000_000),
                                 make_offer("B", stock=1_000_000),
                                 make_offer("C", stock=1_000_000)])
        result = self.score(part)
        self.assertNotEqual(result.risk.level, RiskLevel.LOW)
        self.assertGreaterEqual(result.risk.score, 50)

    def test_nrnd_is_at_least_medium(self):
        part = make_part(lifecycle=Lifecycle.NRND,
                         offers=[make_offer("A", stock=500_000),
                                 make_offer("B", stock=500_000)])
        result = self.score(part)
        self.assertGreaterEqual(result.risk.score, 25)

    def test_single_source_is_flagged(self):
        part = make_part(offers=[make_offer("OnlyOne")])
        result = self.score(part)
        self.assertIn("Single source", result.risk.flags)

    def test_no_stock_is_flagged(self):
        part = make_part(offers=[make_offer("A", stock=0, lead=140)])
        result = self.score(part)
        self.assertIn("No stock", result.risk.flags)

    def test_dnp_line_is_zero_risk(self):
        result = self.score(make_part(), make_line(dnp=True, quantity=1))
        self.assertEqual(result.risk.score, 0.0)
        self.assertIn("DNP", result.risk.flags)

    def test_no_data_has_a_risk_floor(self):
        line = make_line()
        result = LineResult(line=line, part=None)
        result.match = Match(kind=MatchKind.NONE)
        result.risk = risk_mod.score_line(result, self.settings)
        self.assertGreaterEqual(result.risk.score, 45)

    def test_non_rohs_raises_compliance_risk(self):
        part = make_part(compliance=Compliance(
            rohs=ComplianceState.NON_COMPLIANT,
            rohs_note="Contains lead"))
        result = self.score(part)
        factor = next(f for f in result.risk.factors
                      if f.code == "compliance")
        self.assertGreater(factor.score, 60)
        self.assertIn("Not RoHS", result.risk.flags)

    def test_every_factor_has_a_written_explanation(self):
        result = self.score(make_part())
        for factor in result.risk.factors:
            self.assertTrue(factor.detail, factor.code)

    def test_weights_change_the_outcome(self):
        part = make_part(lifecycle=Lifecycle.NRND)
        baseline = self.score(part).risk.score
        self.settings.weight_lifecycle = 6.0
        heavier = self.score(part).risk.score
        self.assertGreater(heavier, baseline)


# --------------------------------------------------------------------------- #
# Alternates
# --------------------------------------------------------------------------- #

class TestAlternates(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.fx = FxTable()

    def score(self, original, candidate, source="similar", line=None):
        return alt_mod.score_alternate(
            original, line or make_line(), candidate, source, self.settings,
            self.fx, required_qty=100)

    def test_drop_in_replacement_scores_well(self):
        original = make_part(lifecycle=Lifecycle.EOL)
        candidate = make_part(mpn="MC1458DR2G", manufacturer="ON Semiconductor",
                              package="SOIC-8",
                              description="IC OPAMP GP 2 CIRCUIT SOIC-8",
                              offers=[make_offer("A"), make_offer("B")])
        scored = self.score(original, candidate, source="substitution")
        self.assertGreater(scored.score, 65)
        self.assertTrue(scored.pin_compatible)

    def test_different_footprint_is_capped(self):
        original = make_part()
        candidate = make_part(mpn="LM358N", package="DIP-8")
        scored = self.score(original, candidate)
        self.assertFalse(scored.pin_compatible)
        self.assertLessEqual(scored.score, 52)
        self.assertTrue(any("footprint" in c.lower()
                            for c in scored.concerns))

    def test_value_mismatch_is_capped_hard(self):
        """Regression: a 0.1uF is not an alternate for a 10uF."""
        original = make_part(mpn="TAJB106K016RNJ", manufacturer="AVX",
                             package="1210",
                             description="CAP TANT 10UF 16V 10% 1210")
        candidate = make_part(mpn="C0603C104K5RACTU", manufacturer="KEMET",
                              package="1210",
                              description="CAP CER 0.1UF 50V X7R 0603")
        scored = self.score(original, candidate,
                            line=make_line(mpn="TAJB106K016RNJ", package="1210"))
        self.assertLessEqual(scored.score, 30)

    def test_bom_declared_alternates_get_a_bonus(self):
        original = make_part()
        candidate = make_part(mpn="MC1458DR2G", package="SOIC-8",
                              description="IC OPAMP GP 2 CIRCUIT SOIC-8")
        from_bom = self.score(original, candidate, source="bom")
        from_search = self.score(original, candidate, source="search")
        self.assertGreater(from_bom.score, from_search.score)

    def test_obsolete_alternate_is_penalised(self):
        original = make_part(lifecycle=Lifecycle.EOL)
        candidate = make_part(mpn="X1", package="SOIC-8",
                              lifecycle=Lifecycle.OBSOLETE,
                              description="IC OPAMP GP 2 CIRCUIT SOIC-8")
        scored = self.score(original, candidate)
        self.assertTrue(any("Obsolete" in c or "obsolete" in c
                            for c in scored.concerns))

    def test_needs_alternates_only_when_useful(self):
        healthy = make_part(offers=[make_offer("A", stock=10 ** 6),
                                    make_offer("B", stock=10 ** 6)])
        self.assertFalse(alt_mod.needs_alternates(healthy, 100, self.settings))
        risky = make_part(lifecycle=Lifecycle.NRND)
        self.assertTrue(alt_mod.needs_alternates(risky, 100, self.settings))
        empty = make_part(offers=[])
        self.assertTrue(alt_mod.needs_alternates(empty, 100, self.settings))

    def test_ranking_and_deduplication(self):
        original = make_part()
        good = make_part(mpn="GOOD", package="SOIC-8",
                         description="IC OPAMP GP 2 CIRCUIT SOIC-8")
        bad = make_part(mpn="BAD", package="DIP-8", offers=[])
        ranked = alt_mod.build_alternates(
            make_line(), original,
            [(good, "similar"), (good, "search"), (bad, "search")],
            self.settings, self.fx, 100)
        self.assertEqual([a.mpn for a in ranked], ["GOOD", "BAD"])

    def test_parametric_query_describes_the_part(self):
        query = alt_mod.parametric_query(
            make_line(value="10k", package="0603"),
            make_part(specs={"Tolerance": "±1%"}))
        self.assertIn("10k", query)


# --------------------------------------------------------------------------- #
# Compliance
# --------------------------------------------------------------------------- #

class TestCompliance(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()

    def result_for(self, compliance):
        part = make_part(compliance=compliance)
        line = make_line()
        result = LineResult(line=line, part=part)
        result.compliance = comp_mod.resolve(part, self.settings)
        return result

    def test_non_compliant_is_an_error_when_required(self):
        result = self.result_for(Compliance(
            rohs=ComplianceState.NON_COMPLIANT))
        issues = comp_mod.line_issues(result, self.settings)
        self.assertTrue(any(i.code == "rohs_non_compliant"
                            and i.severity.value == "error" for i in issues))

    def test_unknown_is_reported_not_assumed_compliant(self):
        result = self.result_for(Compliance(rohs=ComplianceState.UNKNOWN))
        issues = comp_mod.line_issues(result, self.settings)
        self.assertTrue(any(i.code == "rohs_unknown" for i in issues))

    def test_eccn_implies_export_control(self):
        part = make_part(compliance=Compliance(eccn="3A991"))
        resolved = comp_mod.resolve(part, self.settings)
        self.assertTrue(resolved.export_controlled)

    def test_ear99_is_not_export_controlled(self):
        part = make_part(compliance=Compliance(eccn="EAR99"))
        resolved = comp_mod.resolve(part, self.settings)
        self.assertFalse(resolved.export_controlled)

    def test_svhc_is_surfaced(self):
        result = self.result_for(Compliance(
            reach=ComplianceState.NON_COMPLIANT,
            svhc=["Lead monoxide"]))
        issues = comp_mod.line_issues(result, self.settings)
        self.assertTrue(any("Lead monoxide" in i.message for i in issues))

    def test_summary_counts_and_blockers(self):
        results = [
            self.result_for(Compliance(rohs=ComplianceState.COMPLIANT)),
            self.result_for(Compliance(rohs=ComplianceState.NON_COMPLIANT)),
            self.result_for(Compliance(rohs=ComplianceState.UNKNOWN)),
        ]
        summary = comp_mod.summarise(results, self.settings)
        self.assertEqual(summary["rohs"]["Compliant"], 1)
        self.assertEqual(len(summary["blockers"]), 1)
        self.assertEqual(summary["missing_declarations"], 1)
        issues = comp_mod.bom_issues(summary, self.settings)
        self.assertTrue(any(i.code == "compliance_blockers" for i in issues))


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

class TestHealth(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        self.fx = FxTable()

    def result_for(self, part, quantity=1.0, line_no=1):
        line = make_line(line_no=line_no, quantity=quantity)
        result = LineResult(line=line, part=part)
        result.match = match_mod.classify(line, part)
        result.compliance = comp_mod.resolve(part, self.settings)
        result.cost = cost_mod.cost_line(part, quantity, self.settings,
                                         self.fx)
        result.risk = risk_mod.score_line(result, self.settings)
        return result

    def test_healthy_bom_scores_high(self):
        part = make_part(offers=[make_offer("A"), make_offer("B"),
                                 make_offer("C")])
        results = [self.result_for(part, line_no=n) for n in range(1, 6)]
        health = health_mod.compute(results, self.settings)
        self.assertGreater(health.score, 80)
        self.assertIn(health.grade, ("A+", "A", "B+"))

    def test_obsolete_parts_drag_the_score_down(self):
        good = make_part(offers=[make_offer("A"), make_offer("B")])
        bad = make_part(mpn="DEAD", lifecycle=Lifecycle.OBSOLETE, offers=[])
        healthy = health_mod.compute(
            [self.result_for(good, line_no=n) for n in range(1, 5)],
            self.settings)
        mixed = health_mod.compute(
            [self.result_for(good, line_no=1), self.result_for(good, line_no=2),
             self.result_for(bad, line_no=3), self.result_for(bad, line_no=4)],
            self.settings)
        self.assertLess(mixed.score, healthy.score - 10)
        self.assertTrue(mixed.drivers)

    def test_placement_weighting(self):
        good = make_part(offers=[make_offer("A"), make_offer("B")])
        bad = make_part(mpn="DEAD", lifecycle=Lifecycle.OBSOLETE, offers=[])
        few = health_mod.compute(
            [self.result_for(good, quantity=100, line_no=1),
             self.result_for(bad, quantity=1, line_no=2)], self.settings)
        many = health_mod.compute(
            [self.result_for(good, quantity=1, line_no=1),
             self.result_for(bad, quantity=100, line_no=2)], self.settings)
        self.assertLess(many.score, few.score)

    def test_empty_bom(self):
        health = health_mod.compute([], self.settings)
        self.assertEqual(health.grade, "n/a")

    def test_all_dnp_bom(self):
        results = [self.result_for(make_part(), line_no=1)]
        results[0].line.dnp = True
        health = health_mod.compute(results, self.settings)
        self.assertEqual(health.grade, "n/a")

    def test_grades_are_monotonic(self):
        grades = [health_mod.grade_for(score)
                  for score in (95, 86, 80, 72, 65, 58, 48, 20)]
        self.assertEqual(grades, ["A+", "A", "B+", "B", "C+", "C", "D", "E"])

    def test_top_risks_ordering(self):
        good = make_part(offers=[make_offer("A"), make_offer("B")])
        bad = make_part(mpn="DEAD", lifecycle=Lifecycle.OBSOLETE, offers=[])
        results = [self.result_for(good, line_no=1),
                   self.result_for(bad, line_no=2)]
        top = health_mod.top_risks(results)
        self.assertEqual(top[0]["line_no"], 2)


if __name__ == "__main__":
    unittest.main()
