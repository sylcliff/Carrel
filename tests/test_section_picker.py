"""Unit tests for the intent-aware section picker."""
from __future__ import annotations

from carrel.pipeline import _section_picker as picker


def _md(*blocks: tuple[str, str]) -> str:
    """Build a markdown string from (heading, body) pairs.

    No trailing newlines per block; ``render_numbered`` and the
    chunker add their own separators.
    """
    return "\n\n".join(
        (f"# {h}\n\n{b}" if h else b) for h, b in blocks
    )


# --- _is_drop -------------------------------------------------------------


class TestIsDrop:
    def test_drops_references(self):
        assert picker._is_drop("References")

    def test_drops_bibliography_case_insensitive(self):
        assert picker._is_drop("BIBLIOGRAPHY")

    def test_drops_acknowledgments(self):
        assert picker._is_drop("Acknowledgments")
        assert picker._is_drop("Acknowledgement")

    def test_drops_supplementary_variants(self):
        assert picker._is_drop("Supplementary Material")
        assert picker._is_drop("Supplementary Information")
        assert picker._is_drop("Supplementary")

    def test_drops_appendix(self):
        assert picker._is_drop("Appendix A")
        assert picker._is_drop("Appendices")

    def test_drops_funding_boilerplate(self):
        assert picker._is_drop("Funding")
        assert picker._is_drop("Funding Information")
        assert picker._is_drop("Funding Statement")
        assert picker._is_drop("Acknowledgments and Funding")

    def test_drops_author_contributions(self):
        assert picker._is_drop("Author Contributions")

    def test_drops_data_availability(self):
        assert picker._is_drop("Data Availability")

    def test_drops_nested_under_supplementary(self):
        # "Methods" under a Supplementary parent is dropped because
        # the parent matches the DROP pattern.
        assert picker._is_drop("Supplementary / Methods")

    def test_keeps_plain_methods(self):
        assert not picker._is_drop("Methods")
        assert not picker._is_drop("Method")
        assert not picker._is_drop("Methodology")
        assert not picker._is_drop("Our Approach")

    def test_keeps_plain_results(self):
        assert not picker._is_drop("Results")
        assert not picker._is_drop("Experiments")

    def test_empty_heading_not_dropped(self):
        # Preamble / unlabeled sections are kept and classified as
        # priority 99, not dropped.
        assert not picker._is_drop("")


# --- _classify -------------------------------------------------------------


class TestClassify:
    def test_method_ranks_first(self):
        assert picker._classify("Methods") == (1, "Method")
        assert picker._classify("Method") == (1, "Method")
        assert picker._classify("Methodology") == (1, "Method")
        assert picker._classify("Our Approach") == (1, "Method")
        assert picker._classify("Proposed Method") == (1, "Method")

    def test_results_ranks_second(self):
        assert picker._classify("Results") == (2, "Results")
        assert picker._classify("Experiments") == (2, "Results")
        assert picker._classify("Performance Evaluation") == (2, "Results")
        assert picker._classify("Ablation Study") == (2, "Results")

    def test_conclusion_ranks_third(self):
        assert picker._classify("Conclusion") == (3, "Conclusion")
        assert picker._classify("Discussion") == (3, "Conclusion")
        assert picker._classify("Limitations") == (3, "Conclusion")
        assert picker._classify("Future Work") == (3, "Conclusion")

    def test_intro_ranks_fourth(self):
        assert picker._classify("Introduction") == (4, "Intro")
        assert picker._classify("Related Work") == (4, "Intro")
        assert picker._classify("Background") == (4, "Intro")
        assert picker._classify("Preliminaries") == (4, "Intro")

    def test_unranked_goes_last(self):
        # A section that doesn't match any rule still gets a
        # priority 99 (kept, sorted to the end) so content isn't lost
        # silently.
        assert picker._classify("Section 3") == (99, "Section 3")

    def test_empty_heading_is_unranked_body(self):
        assert picker._classify("") == (99, "Body")

    def test_nested_uses_leaf(self):
        # "Experiments / Methods" → leaf is "Methods" → priority 1.
        assert picker._classify("Experiments / Methods") == (1, "Method")


# --- select_sections -------------------------------------------------------


class TestSelectSections:
    def test_drops_references_and_acknowledgments(self):
        md = _md(
            ("Introduction", "intro body"),
            ("Methods", "method body"),
            ("Results", "result body"),
            ("References", "[1] Foo, [2] Bar"),
            ("Acknowledgments", "thanks"),
        )
        picked = picker.select_sections(md, budget_chars=10_000)
        flat = " ".join(b for _p, _o, _l, b in picked)
        assert "[1] Foo" not in flat
        assert "thanks" not in flat
        labels = [label for _p, _o, label, _b in picked]
        assert "Method" in labels
        assert "Results" in labels

    def test_output_preserves_document_order(self):
        # The picker walks candidates in priority order to fill the
        # budget, but the final output must be in document order so
        # the LLM reads the paper in its natural sequence.  This is
        # the fix for the "Methods first, then Results, then
        # Conclusion, then truncated Intro" smell — a paper that
        # leads with Abstract + Intro should keep that order.
        md = _md(
            ("Introduction", "intro " * 50),
            ("Methods", "method " * 50),
            ("Results", "result " * 50),
            ("Conclusion", "conclusion " * 50),
        )
        picked = picker.select_sections(md, budget_chars=10_000)
        labels = [p[2] for p in picked]
        assert labels == ["Intro", "Method", "Results", "Conclusion"]
        # ``order`` is the section's index in the document; the
        # output sequence must be monotonically increasing.
        orders = [p[1] for p in picked]
        assert orders == sorted(orders)

    def test_priority_fill_then_doc_order_output(self):
        # Two-section paper where Conclusion (priority 3) is the
        # FIRST section in the document and Methods (priority 1)
        # is the SECOND.  Both fit in budget.  The output must
        # follow document order (Conclusion before Methods), but
        # the budget-fill walk should have considered Methods
        # first by priority — i.e. both must be picked, neither
        # truncated.  This locks in: priority drives which
        # sections are KEPT; document order drives the OUTPUT
        # sequence.
        conclusion = "c " * 200   # 400 chars
        method = "m " * 200       # 400 chars
        md = _md(
            ("Conclusion", conclusion),
            ("Methods", method),
        )
        picked = picker.select_sections(md, budget_chars=2_000)
        # Both kept (well under budget, no truncation).
        labels = [p[2] for p in picked]
        assert labels == ["Conclusion", "Method"]
        # Document-order index on the tuple proves the sort.
        orders = [p[1] for p in picked]
        assert orders == [0, 1]

    def test_budget_cap_stops_picking(self):
        # Each body is ~500 chars; a budget of 600 only fits Methods
        # because the picker treats the remaining 100 chars as "not
        # worth a half-section" and breaks.
        md = _md(
            ("Methods", "m " * 250),
            ("Results", "r " * 250),
            ("Conclusion", "c " * 250),
        )
        picked = picker.select_sections(md, budget_chars=600)
        assert len(picked) == 1
        assert picked[0][2] == "Method"

    def test_budget_admits_truncated_when_remaining_is_meaningful(self):
        # 200+ chars of remaining budget is enough to be worth a
        # truncated second section. The picker trims Results to
        # fit and keeps the doc order.
        md = _md(
            ("Methods", "m " * 250),
            ("Results", "r " * 250),
        )
        picked = picker.select_sections(md, budget_chars=800)
        labels = [p[2] for p in picked]
        assert labels == ["Method", "Results"]
        # Truncation marker confirms the second was clipped.
        assert picked[1][3].endswith("…")

    def test_per_section_cap_truncates(self):
        # Method body is 2000 chars; per-section cap of 500 means
        # the picker keeps only the first 500 chars of that section.
        md = _md(
            ("Methods", "x" * 2000),
            ("Results", "r " * 50),
        )
        picked = picker.select_sections(
            md, budget_chars=10_000, per_section_cap=500
        )
        method_pick = next(p for p in picked if p[2] == "Method")
        # The truncation marker adds a couple chars; allow some slack.
        assert len(method_pick[3]) <= 520
        assert method_pick[3].endswith("…")

    def test_empty_input(self):
        assert picker.select_sections("", budget_chars=1000) == []
        assert picker.select_sections("   \n\n  ", budget_chars=1000) == []

    def test_drops_empty_sections(self):
        md = "# Methods\n\n\n\n# Results\n\nactual content"
        picked = picker.select_sections(md, budget_chars=10_000)
        # The empty Methods section shouldn't be picked.
        labels = [p[2] for p in picked]
        assert labels == ["Results"]

    def test_strips_minerU_image_lines(self):
        md = (
            "# Methods\n\n"
            "![]()\n"
            "real method text\n"
            "![caption](img.png)\n"
            "more method text\n"
        )
        picked = picker.select_sections(md, budget_chars=10_000)
        method_body = picked[0][3]
        assert "![]" not in method_body
        assert "![caption]" not in method_body
        assert "real method text" in method_body
        assert "more method text" in method_body


# --- render_numbered -------------------------------------------------------


class TestRenderNumbered:
    def test_format_is_numbered_heading_then_body(self):
        md = _md(
            ("Methods", "method body"),
            ("Results", "result body"),
        )
        picked = picker.select_sections(md, budget_chars=10_000)
        out = picker.render_numbered(picked)
        assert "## [1] Method" in out
        assert "## [2] Results" in out
        assert out.index("## [1] Method") < out.index("method body")
        assert out.index("## [2] Results") < out.index("result body")

    def test_empty_input_renders_empty(self):
        assert picker.render_numbered([]) == ""


# --- prepare_picker_input (end-to-end) ------------------------------------


class TestPreparePickerInput:
    def test_priority_picks_method_first(self):
        md = _md(
            ("Introduction", "intro " * 100),
            ("Methods", "method " * 100),
            ("Results", "result " * 100),
            ("Conclusion", "conclusion " * 100),
            ("References", "[1] Foo"),
        )
        out = picker.prepare_picker_input(md, budget_chars=2000)
        # The numbered list is contiguous; [1] is always Method, [2+]
        # is the rest.
        assert out.startswith("## [1] Method")
        # References must be absent.
        assert "[1] Foo" not in out
        assert "References" not in out

    def test_budget_respected(self):
        md = _md(
            ("Methods", "m " * 5000),
            ("Results", "r " * 5000),
        )
        out = picker.prepare_picker_input(md, budget_chars=3000)
        # Body should not wildly exceed budget; the picker includes
        # "## [1] Method" label (~14 chars) so the total can be a
        # hair over. Allow 10% slack.
        assert len(out) < 3000 * 1.1

    def test_fallback_when_no_headings(self):
        # A blob with no ATX headings still has its whole body
        # classified as priority 99 (label "Body") and rendered as
        # a single numbered block — there's no char-window fallback
        # because ``split_by_heading`` always yields one section.
        md = "lorem ipsum " * 500
        out = picker.prepare_picker_input(md, budget_chars=1000)
        assert out.startswith("## [1] Body")
        # Truncation marker shows the per-section cap kicked in
        # (5000 chars of body < 1000 budget → trimmed).
        assert "…" in out
        assert len(out) < 1200

    def test_empty_md_returns_empty(self):
        assert picker.prepare_picker_input("", budget_chars=1000) == ""
        assert picker.prepare_picker_input("   ", budget_chars=1000) == ""
