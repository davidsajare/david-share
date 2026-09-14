"""Offline reader, evidence, bilingual-shape and source-sync gates for Task B.

This is a deterministic documentation gate, not an independent linguistic review
or proof that cloud resources have been stopped.
Direct isolated check: python -I -S scripts/validate_router_readme.py --self-test
"""

from __future__ import annotations

import argparse
import ast
import copy
import re
import subprocess
import sys
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_router_readme as builder

ROOT = builder.ROOT
PENDING_EVIDENCE = (
    f"outputs/raw_fulltext/router_{builder.RUN}.jsonl",
    f"outputs/raw_fulltext/quality_{builder.RUN}.jsonl",
    "outputs/resource_closeout_router.json",
)
ANCHORS = {"start", "findings", "architecture", "results", "cases", "example",
           "reproduce", "live", "limits", "evidence", "sources"}
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```$", re.MULTILINE | re.DOTALL)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def blocks(text: str) -> list[tuple[str, str]]:
    result = FENCE.findall(text)
    require(sum(line.startswith("```") for line in text.splitlines()) == 2 * len(result),
            "Unbalanced or malformed code fences")
    return result


def tables(text: str) -> list[list[list[str]]]:
    result = []
    current = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("|"):
            require(line.endswith("|"), "Unterminated table row")
            current.append([c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]])
        elif current:
            require(len(current) >= 3, "Table has no data")
            require(all(len(row) == len(current[0]) for row in current), "Ragged table")
            require(all(re.fullmatch(r":?-+:?", cell) for cell in current[1]), "Invalid table separator")
            result.append(current)
            current = []
    return result


def validate_links(text: str, root: Path, documents: dict[str, str]) -> None:
    for target in LINK.findall(text):
        url = urlsplit(target)
        if url.scheme:
            require(url.scheme == "https", f"Unexpected link scheme: {target}")
            continue
        require(not url.netloc and not url.path.startswith(("/", "\\")),
                f"Non-relative local link: {target}")
        path = unquote(url.path)
        if path:
            resolved = (root / path).resolve()
            project_root = root.parent.resolve()
            require(
                resolved.is_relative_to(project_root),
                f"Link escapes the independent benchmark project: {target}",
            )
            require(path in documents or resolved.is_file(), f"Broken local link: {target}")
        if url.fragment:
            linked_text = documents.get(path) if path else text
            if linked_text is None and path.endswith(".md"):
                linked_text = (root / path).read_text(encoding="utf-8")
            require(linked_text is not None and f'id="{url.fragment}"' in linked_text,
                    f"Missing explicit local anchor: {target}")


def normalized_link_counter(text: str) -> Counter[str]:
    """Compare bilingual destinations while treating README-CN as its peer."""
    return Counter(
        target.replace("README-CN.md", "README.md")
        for target in LINK.findall(text)
    )


def validate_source_contract(root: Path) -> None:
    harness = (root / "harness.py").read_text(encoding="utf-8")
    judge = (root / "judge.py").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    for needle in ('"2025-04-01-preview"', '"Foundry-Features": "ModelRouterControls=V1Preview"',
                   "DefaultAzureCredential()", 'choices=["paygo", "ptu"]'):
        require(needle in harness, f"Harness source contract changed: {needle}")
    require('p.add_argument("--max-chars", type=int, default=0' in judge,
            "Judge clipping default no longer matches the README")
    require('p.add_argument("--dataset"' in judge and "Prefer the earliest measured iteration" in judge,
            "Judge dataset/selection contract changed")
    require(set(requirements.splitlines()) == {"openai==3.10.0", "azure-identity==1.25.3"},
            "Pinned dependency documentation must be updated")
    tree = ast.parse(harness)
    system_assignments = [
        node for node in tree.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "SYSTEM_MSG" for target in node.targets)
    ]
    require(len(system_assignments) == 1
            and ast.literal_eval(system_assignments[0].value) == builder.SYSTEM_MESSAGE,
            "Shared system message differs from the harness")
    expected_messages = ast.parse(
        '[{"role": "system", "content": SYSTEM_MSG}, {"role": "user", "content": item["text"]}]',
        mode="eval",
    ).body
    require(any(
        isinstance(node, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value == "messages"
            and ast.dump(value) == ast.dump(expected_messages)
            for key, value in zip(node.keys, node.values)
        ) for node in ast.walk(tree)
    ), "Single-turn Chat system/user message structure changed")
    preflight = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "preflight")
    require(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "measure_rtt"
                for node in ast.walk(preflight)), "Preflight network description needs revalidation")


def validate_documents(documents: dict[str, str], root: Path = ROOT,
                       expected: dict[str, str] | None = None) -> None:
    require(set(documents) == {"README.md", "README-CN.md"}, "Both READMEs are required")
    if expected is not None:
        require(documents == expected, "README content differs from the verified generator")
    en, cn = documents["README.md"], documents["README-CN.md"]
    for name, text in documents.items():
        require(150 <= len(text.splitlines()) <= 220, f"{name}: outside the 150–220 line reader budget")
        require(text.endswith("\n"), f"{name}: missing terminal newline")
        anchors = re.findall(r'<a id="([^"]+)"></a>', text)
        require(set(anchors) == ANCHORS and len(anchors) == len(ANCHORS), f"{name}: wrong or duplicate anchors")
        require(len(re.findall(r"^## ", text, re.MULTILINE)) <= 10, f"{name}: too many primary sections")
        validate_links(text, root, documents)
        code = blocks(text)
        require(("text", builder.SYSTEM_MESSAGE + "\n") in code,
                f"{name}: exact shared system message is missing or altered")
        bash = [body for lang, body in code if lang == "bash"]
        require(any(builder.LIVE_COMMAND + "\n" == body for body in bash), f"{name}: main live command drift")
        require(any(builder.LIVE_COMMAND + " --preflight\n" == body for body in bash), f"{name}: preflight drift")
        require('python judge.py "outputs/router_${RUN_ID}.jsonl" --dataset datasets/router_taskb.jsonl '
                '--judge-deployment judge-terra --max-chars 0' in text, f"{name}: judge command drift")
        for command in ("python scripts/build_router_report.py", "python scripts/verify_router_fulltext.py",
                        "python scripts/build_router_readme.py", "python scripts/validate_router_readme.py",
                        "python -m unittest discover -s tests"):
            require(command in text, f"{name}: missing reproduction stage: {command}")
        require("model_selection_details.model_router_details" in text, f"{name}: no trace location")
        require("NOT TESTED" in text and "APIM" in text, f"{name}: future-work boundary missing")
        offline, live = text.split('<a id="live"></a>', 1)
        require("Python 3.10+" in offline and "python3 -m venv --without-pip .venv-offline" in offline,
                f"{name}: offline standard-library setup is missing")
        require("pip install" not in offline, f"{name}: offline path must not require pip installation")
        require(text.count("python -m pip install -r requirements.txt") == 1
                and "python -m pip install -r requirements.txt" in live,
                f"{name}: SDK installation must appear only in the live path")
        require("Windows" in live and "NOT VERIFIED" in live and "TLS" in live,
                f"{name}: local Windows SDK verification boundary is missing")
        require("Xinyu Wei (魏新宇)" in text and "2026-09-10" in text, f"{name}: authorship/date drift")
        require([lang for lang, _ in code].count("mermaid") == 1, f"{name}: expected one architecture diagram")
        ts = tables(text)
        require([len(rows) - 2 for rows in ts] == [3, 10, 6, 6, 6, 6, 7],
                f"{name}: reader tables lost a goal, arm, mode, case, scenario or evidence row")
    require(re.findall(r"^(#+) ", en, re.MULTILINE) == re.findall(r"^(#+) ", cn, re.MULTILINE),
            "Bilingual heading hierarchy drift")
    require(blocks(en) == blocks(cn), "Bilingual code, prompt or diagram drift")
    require(
        normalized_link_counter(en) == normalized_link_counter(cn),
        "Bilingual evidence/link drift",
    )
    require([[len(row) for row in ts] for ts in tables(en)] == [[len(row) for row in ts] for ts in tables(cn)],
            "Bilingual table shape drift")
    require(re.findall(r"!\[[^\]]*\]\(([^)]+)\)", en) == re.findall(r"!\[[^\]]*\]\(([^)]+)\)", cn),
            "Bilingual image order drift")
    require(Counter(re.findall(r"\d+(?:[.,]\d+)*(?:%)?", en)) ==
            Counter(re.findall(r"\d+(?:[.,]\d+)*(?:%)?", cn)), "Bilingual numeric-token drift")


class DocumentationGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = builder.build_documents()

    def test_current_generated_contract(self) -> None:
        validate_documents(self.expected, expected=self.expected)
        validate_source_contract(ROOT)

    def test_links_may_reach_a_sibling_but_not_leave_the_project(self) -> None:
        validate_links(
            "[scenario](../qira-scenario-model-benchmark/README.md)",
            ROOT,
            {},
        )
        with self.assertRaisesRegex(ValueError, "escapes the independent benchmark"):
            validate_links("[escape](../../README.md)", ROOT, {})

    def test_bilingual_readme_counterparts_normalize_to_one_link(self) -> None:
        english = "[study](../study/README.md)"
        chinese = "[研究](../study/README-CN.md)"
        self.assertEqual(
            normalized_link_counter(english),
            normalized_link_counter(chinese),
        )

    def test_direct_isolated_entry_points_from_unrelated_directory(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="router-isolated-cli-") as directory:
            for name, arguments in (
                ("build_router_readme.py", ["--check"]),
                ("validate_router_readme.py", []),
            ):
                with self.subTest(script=name):
                    result = subprocess.run(
                        [sys.executable, "-I", "-S", "-B", str(ROOT / "scripts" / name), *arguments],
                        cwd=directory, capture_output=True, text=True, timeout=120,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("VERIFIED:", result.stdout)

    def test_final_routing_counts_and_stability(self) -> None:
        evidence = builder.load_evidence()
        for mode, expected_sol in (("balanced", 6), ("cost", 0), ("quality", 69)):
            for suffix in ("", "@low"):
                row = evidence.tiers[(f"router-sol-luna-{mode}{suffix}", "ALL")]
                self.assertEqual(builder.numeric(row, "requests"), 141)
                self.assertEqual(builder.numeric(row, "sol_requests"), expected_sol)
        self.assertEqual(len(evidence.hits), 282)
        self.assertEqual(sum(row["consistent"] == "False" for row in evidence.hits.values()), 0)

    def test_final_cost_and_latency_scope(self) -> None:
        evidence = builder.load_evidence()
        self.assertEqual(evidence.all_requests, 1880)
        self.assertEqual(len(evidence.measured), 1410)
        self.assertAlmostEqual(evidence.all_cost_usd, 7.4057616, places=7)
        trace = [r["router_latency_ms"] for r in evidence.measured if r.get("router_latency_ms") is not None]
        self.assertEqual(builder.report.analyze.pct(trace, 50), 20)
        self.assertEqual(builder.report.analyze.pct(trace, 95), 24)
        self.assertEqual({r["network_rtt_ms"] for r in evidence.measured}, {4.26})

    def test_direct_burst_counts_and_spans(self) -> None:
        evidence = builder.load_evidence()
        direct = [r for r in evidence.measured if r["arm"] in builder.ARMS[6:]]
        self.assertEqual(len(direct), 564)
        self.assertEqual(sum(r.get("decode_ms") is not None and r["decode_ms"] < 50 for r in direct), 497)
        self.assertEqual(sum(builder.numeric(evidence.arms[a], "decode_span_lt50ms_requests")
                             for a in builder.ARMS[6:]), 497)
        self.assertEqual([builder.numeric(evidence.arms[a], "decode_span_p50_ms")
                          for a in builder.ARMS[6:]], [9.9, 9.2, 10.8, 10.3])
        for text in self.expected.values():
            self.assertIn("497/564", text)
            self.assertIn("**<50 ms**", text)
            self.assertFalse(any("decode_tps" in cell for rows in tables(text) for row in rows for cell in row))
        self.assertIn("The responsible layer/cause was not diagnosed.", self.expected["README.md"])
        self.assertIn("造成该现象的层级与原因尚未诊断。", self.expected["README-CN.md"])

    def test_qira_only_scenario_matrix(self) -> None:
        evidence = builder.load_evidence()
        self.assertEqual(len(evidence.scenarios), 60)
        self.assertEqual(sum(item["source"] == "qira_scenarios" for item in evidence.dataset.values()), 17)
        self.assertEqual(sum(item["source"] == "router_questions" for item in evidence.dataset.values()), 30)
        for arm in builder.ARMS:
            rows = [row for (a, _), row in evidence.scenarios.items() if a == arm]
            self.assertEqual(len(rows), 6)
            self.assertEqual(sum(builder.numeric(row, "questions") for row in rows), 17)
            self.assertEqual(sum(builder.numeric(row, "requests") for row in rows), 51)
        for text in self.expected.values():
            self.assertIn("(outputs/router_qira_scenarios.csv)", text)

    def test_offline_pip_dependency_mutation(self) -> None:
        changed = {
            name: text.replace("python3 -m venv --without-pip .venv-offline",
                               "python3 -m venv --without-pip .venv-offline\npython -m pip install example-package")
            for name, text in self.expected.items()
        }
        with self.assertRaisesRegex(ValueError, "offline path must not require pip"):
            validate_documents(changed)

    def rejected(self, old: str, new: str, name: str = "README-CN.md") -> None:
        changed = dict(self.expected)
        self.assertIn(old, changed[name])
        changed[name] = changed[name].replace(old, new, 1)
        with self.assertRaises(ValueError):
            validate_documents(changed)

    def test_bilingual_number_mutation(self) -> None:
        self.rejected("1,410", "1,411")

    def test_effort_command_mutation(self) -> None:
        self.rejected("--efforts=-,low", "--efforts=none,low")

    def test_broken_link_mutation(self) -> None:
        self.rejected("(outputs/router_arm_summary.csv)", "(outputs/nonexistent_router_summary.csv)")

    def test_missing_arm_mutation(self) -> None:
        changed = dict(self.expected)
        changed["README.md"] = "\n".join(line for line in changed["README.md"].split("\n")
                                       if not line.startswith("| gpt-5.6-sol-dz | low |"))
        with self.assertRaises(ValueError):
            validate_documents(changed)

    def test_missing_anchor_mutation(self) -> None:
        self.rejected('<a id="reproduce"></a>', '<a id="reproduction"></a>')

    def test_prompt_mutation(self) -> None:
        self.rejected("nondeterministic output", "deterministic output")

    def test_missing_system_prompt_mutation(self) -> None:
        self.rejected(builder.SYSTEM_MESSAGE, "")

    def test_system_prompt_mutation_even_when_bilingual(self) -> None:
        changed = {
            name: text.replace(builder.SYSTEM_MESSAGE, "You are a helpful assistant.")
            for name, text in self.expected.items()
        }
        with self.assertRaisesRegex(ValueError, "shared system message"):
            validate_documents(changed)

    def test_claim_mutation_even_when_bilingual(self) -> None:
        changed = {name: text.replace("DataZoneStandard", "GlobalStandard") for name, text in self.expected.items()}
        with self.assertRaisesRegex(ValueError, "verified generator"):
            validate_documents(changed, expected=self.expected)

    def test_nonfinite_csv_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            builder.numeric({"sol_share": "nan"}, "sol_share")

    def test_duplicate_csv_cells_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            builder.keyed([{"arm": "a"}, {"arm": "a"}], ("arm",), {"a"})

    def test_csv_metric_drift_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "differs from archive"):
            builder.agrees({"arm": "test", "requests": "142"}, "requests", 141)

    def test_absent_final_evidence_does_not_write(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="router-doc-gate-") as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "no READMEs written"):
                builder.build_documents(root)
            self.assertEqual(list(root.iterdir()), [])

    def test_deterministic_render(self) -> None:
        evidence = builder.load_evidence()
        self.assertEqual(builder.render(evidence, "en"), self.expected["README.md"])
        self.assertEqual(builder.render(copy.deepcopy(evidence), "cn"), self.expected["README-CN.md"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--self-test", action="store_true", help="also run focused positive and mutation tests")
    parser.add_argument("--require-complete-evidence", action="store_true",
                        help="also require the retained answers, original scores and closeout record to exist")
    args = parser.parse_args()
    expected = builder.build_documents()
    actual = {name: (ROOT / name).read_text(encoding="utf-8") for name in expected}
    validate_documents(actual, expected=expected)
    validate_source_contract(ROOT)
    pending = [path for path in PENDING_EVIDENCE if not (ROOT / path).is_file()]
    if args.require_complete_evidence:
        require(not pending, "Evidence lifecycle is incomplete: " + ", ".join(pending))
    print("VERIFIED: evidence-derived values, generated content, bilingual shape, commands, local links and anchors.")
    if pending:
        print("PENDING (not claimed complete): " + ", ".join(pending))
    print("NOT VERIFIED by this gate: independent bilingual linguistic review, live Azure execution, resource shutdown.")
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DocumentationGateTests))
        if not result.wasSuccessful():
            raise SystemExit(1)


if __name__ == "__main__":
    main()
