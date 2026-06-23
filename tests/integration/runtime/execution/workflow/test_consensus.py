import unittest

from crewplane.runtime.execution import (
    check_consensus,
)
from crewplane.runtime.execution.consensus import (
    evaluate_review_output,
    extract_verdict,
)
from tests.integration.runtime.execution.workflow.workflow_execution_helpers import (
    review_output,
)


class ExecutorConsensusTests(unittest.TestCase):
    def test_check_consensus_rejects_plain_language_blocker(self) -> None:
        self.assertFalse(check_consensus(["Needs changes before merge."]))

    def test_check_consensus_accepts_plain_language_approval(self) -> None:
        self.assertTrue(check_consensus(["LGTM"]))

    def test_check_consensus_accepts_negated_blocker_approval_language(self) -> None:
        self.assertTrue(check_consensus(["LGTM, no blocking issues."]))
        self.assertTrue(check_consensus(["Ready to merge, no blockers."]))

    def testextract_verdict_accepts_no_findings_contract(self) -> None:
        self.assertEqual(
            extract_verdict(review_output(verdict="NO_FINDINGS")),
            "NO_FINDINGS",
        )

    def testextract_verdict_accepts_changes_requested_contract(self) -> None:
        self.assertEqual(
            extract_verdict(
                review_output(
                    major="- Missing regression tests for billing flow",
                    verdict="CHANGES_REQUESTED",
                )
            ),
            "CHANGES_REQUESTED",
        )

    def testextract_verdict_accepts_nits_only_contract(self) -> None:
        self.assertEqual(
            extract_verdict(
                review_output(
                    nitpicks="- Tighten the naming in the final summary section",
                    verdict="NITS_ONLY",
                )
            ),
            "NITS_ONLY",
        )

    def testextract_verdict_rejects_malformed_review_contract(self) -> None:
        self.assertIsNone(extract_verdict("Summary\n---\nVERDICT: NO_FINDINGS"))

    def test_check_consensus_requires_all_reviewers_to_approve(self) -> None:
        self.assertFalse(
            check_consensus(
                [
                    review_output(verdict="NO_FINDINGS"),
                    review_output(
                        minor="- Add coverage for the unhappy path",
                        verdict="CHANGES_REQUESTED",
                    ),
                ]
            )
        )

    def test_check_consensus_accepts_no_findings_and_nits_only(self) -> None:
        self.assertTrue(
            check_consensus(
                [
                    review_output(verdict="NO_FINDINGS"),
                    review_output(
                        nitpicks="- Clarify the section title",
                        verdict="NITS_ONLY",
                    ),
                ]
            )
        )

    def test_check_consensus_normalizes_contradictory_no_findings_output(self) -> None:
        result = evaluate_review_output(
            review_output(
                nitpicks="- Rename the subsection",
                verdict="NO_FINDINGS",
            )
        )
        self.assertTrue(result.approved)
        self.assertEqual(result.verdict, "NITS_ONLY")

    def test_evaluate_review_output_accepts_leading_preamble(self) -> None:
        raw_output = "\n".join(
            [
                "Now let me check the remaining changed files.",
                "",
                "I have compiled my findings.",
                "",
                review_output(
                    nitpicks="- Tighten the naming in the final summary section",
                    verdict="NITS_ONLY",
                ),
            ]
        )

        result = evaluate_review_output(raw_output)

        self.assertTrue(result.approved)
        self.assertEqual(result.verdict, "NITS_ONLY")
        self.assertTrue(result.had_leading_text)
        self.assertEqual(result.warnings, ())

    def test_evaluate_review_output_accepts_trailing_commentary(self) -> None:
        result = evaluate_review_output(
            review_output(verdict="NO_FINDINGS") + "\nExtra trailing note.\n"
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.verdict, "NO_FINDINGS")
        self.assertTrue(result.had_trailing_text)
        self.assertIn("Ignored commentary below", result.warnings[0])

    def test_evaluate_review_output_repairs_missing_major_for_nits_only(
        self,
    ) -> None:
        result = evaluate_review_output(
            "\n".join(
                [
                    "## Minor Issues",
                    "None",
                    "",
                    "## Nitpicks",
                    "- Tighten the final section title.",
                    "",
                    "---",
                    "VERDICT: NITS_ONLY",
                    "",
                ]
            )
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.verdict, "NITS_ONLY")
        self.assertEqual(result.major_issues, "None")
        self.assertEqual(result.minor_issues, "None")
        self.assertEqual(result.unresolved_issue_count, 0)
        self.assertIn("missing Major Issues section", result.warnings[0])

    def test_evaluate_review_output_ignores_h3_contract_lookalike(
        self,
    ) -> None:
        result = evaluate_review_output(
            "\n".join(
                [
                    "### Major Issues",
                    "- This commentary heading is not part of the final contract.",
                    "",
                    review_output(
                        major="- Real final issue.",
                        verdict="CHANGES_REQUESTED",
                    ),
                ]
            )
        )

        self.assertFalse(result.approved)
        self.assertEqual(result.verdict, "CHANGES_REQUESTED")
        self.assertEqual(result.major_issues, "- Real final issue.")
        self.assertNotIn("commentary heading", result.major_issues)

    def test_evaluate_review_output_does_not_repair_ambiguous_prior_h2(
        self,
    ) -> None:
        result = evaluate_review_output(
            "\n".join(
                [
                    "## Major Issues",
                    "- This exact H2 section appeared before the final block.",
                    "",
                    "## Notes",
                    "The final block starts below.",
                    "",
                    "## Nitpicks",
                    "- Tighten the final section title.",
                    "",
                    "---",
                    "VERDICT: NITS_ONLY",
                    "",
                ]
            )
        )

        self.assertFalse(result.approved)
        self.assertIsNone(result.verdict)
        self.assertEqual(result.evaluation_kind, "unstructured_feedback")
        self.assertEqual(result.original_verdict, "NITS_ONLY")
        self.assertEqual(result.unresolved_issue_count, 0)

    def test_evaluate_review_output_rejects_interstitial_commentary_before_verdict(
        self,
    ) -> None:
        result = evaluate_review_output(
            "\n".join(
                [
                    "## Major Issues",
                    "None",
                    "",
                    "## Minor Issues",
                    "None",
                    "",
                    "## Nitpicks",
                    "None",
                    "",
                    "---",
                    "",
                    "Implementation summary here.",
                    "",
                    "---",
                    "VERDICT: NO_FINDINGS",
                    "",
                ]
            )
        )

        self.assertFalse(result.approved)
        self.assertIsNone(result.verdict)
        self.assertEqual(result.evaluation_kind, "unstructured_feedback")
        self.assertEqual(result.original_verdict, "NO_FINDINGS")
        self.assertEqual(result.unresolved_issue_count, 0)
        self.assertIn("raw reviewer feedback", result.normalized_markdown)

    def test_evaluate_review_output_infers_plain_language_nits_only(self) -> None:
        result = evaluate_review_output(
            "LGTM overall. One optional nitpick: rename the helper for clarity."
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.verdict, "NITS_ONLY")
        self.assertIn("optional nitpick", result.normalized_markdown)

    def test_evaluate_review_output_infers_plain_language_nits_with_no_blockers(
        self,
    ) -> None:
        result = evaluate_review_output(
            "Looks good to me, no blockers. One optional polish item remains."
        )

        self.assertTrue(result.approved)
        self.assertEqual(result.verdict, "NITS_ONLY")

    def test_evaluate_review_output_exposes_sections_and_unresolved_fingerprints(
        self,
    ) -> None:
        result = evaluate_review_output(
            review_output(
                major="- Add a regression test for the retry path",
                minor="- Update the README example",
                nitpicks="- Tighten the summary heading",
                verdict="CHANGES_REQUESTED",
            )
        )

        self.assertEqual(
            result.major_issues, "- Add a regression test for the retry path"
        )
        self.assertEqual(result.minor_issues, "- Update the README example")
        self.assertEqual(result.nitpicks, "- Tighten the summary heading")
        self.assertEqual(len(result.unresolved_fingerprints), 2)
        self.assertEqual(result.unresolved_issue_count, 2)

    def test_evaluate_review_output_keeps_reference_fingerprints_across_wording_drift(
        self,
    ) -> None:
        first = evaluate_review_output(
            review_output(
                major=(
                    "- Fix breaker accounting in "
                    "`apps/llm/domain/services/generation_service.py:100-120`."
                ),
                verdict="CHANGES_REQUESTED",
            )
        )
        second = evaluate_review_output(
            review_output(
                major=(
                    "- Stream breaker handling in "
                    "`apps/llm/domain/services/generation_service.py:140-170` "
                    "is still incorrect."
                ),
                verdict="CHANGES_REQUESTED",
            )
        )

        self.assertTrue(
            set(first.unresolved_fingerprints).intersection(
                second.unresolved_fingerprints
            )
        )

    def test_evaluate_review_output_bounds_reference_fingerprints(self) -> None:
        result = evaluate_review_output(
            review_output(
                major=(
                    "- Fix alpha beta gamma delta epsilon zeta theta iota kappa "
                    "lambda in `src/a.py:10`, `src/b.py:20`, and `src/c.py:30`."
                ),
                verdict="CHANGES_REQUESTED",
            )
        )

        self.assertEqual(result.unresolved_issue_count, 1)
        self.assertLessEqual(len(result.unresolved_fingerprints), 10)

    def test_evaluate_review_output_does_not_match_unrelated_same_file_references(
        self,
    ) -> None:
        first = evaluate_review_output(
            review_output(
                major="- Add regression coverage in `src/app.py:10-20`.",
                verdict="CHANGES_REQUESTED",
            )
        )
        second = evaluate_review_output(
            review_output(
                major="- Rename the helper in `src/app.py:200-220` for clarity.",
                verdict="CHANGES_REQUESTED",
            )
        )

        self.assertFalse(
            set(first.unresolved_fingerprints).intersection(
                second.unresolved_fingerprints
            )
        )

    def test_evaluate_review_output_does_not_match_nit_inside_other_words(self) -> None:
        result = evaluate_review_output("Looks good initially, ready to merge.")

        self.assertTrue(result.approved)
        self.assertEqual(result.verdict, "NO_FINDINGS")

    def test_evaluate_review_output_leaves_ambiguous_unstructured_review_unapproved(
        self,
    ) -> None:
        result = evaluate_review_output("Please double-check the edge cases here.")

        self.assertFalse(result.approved)
        self.assertIsNone(result.verdict)
        self.assertEqual(result.evaluation_kind, "unstructured_feedback")
        self.assertEqual(
            result.unstructured_feedback, "Please double-check the edge cases here."
        )
        self.assertEqual(result.unresolved_issue_count, 0)

    def test_evaluate_review_output_treats_malformed_structured_block_as_nonapproval(
        self,
    ) -> None:
        result = evaluate_review_output(
            "\n".join(
                [
                    "## Major Issues",
                    "None",
                    "",
                    "## Minor Issues",
                    "",
                    "## Nitpicks",
                    "None",
                    "",
                    "---",
                    "VERDICT: NO_FINDINGS",
                    "",
                ]
            )
        )

        self.assertFalse(result.approved)
        self.assertIsNone(result.verdict)
        self.assertEqual(result.evaluation_kind, "unstructured_feedback")
        self.assertEqual(result.original_verdict, "NO_FINDINGS")
        self.assertEqual(result.major_issues, "None")
        self.assertEqual(result.minor_issues, "None")
        self.assertEqual(result.unresolved_issue_count, 0)
