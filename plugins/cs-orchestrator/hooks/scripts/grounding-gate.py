#!/usr/bin/env python3
"""Grounding gate, Stop hook (harness "enforcement").

Verifies the orchestrator's final answer doesn't assert churn scores, ARR, ticket
counts, or utilization figures that don't exist in the CS-stack fixtures/tool data.
This mirrors the operational-trust principle: a CS tool people act on must not
surface a fabricated risk number.

Behaviour:
- Reads the agent transcript from stdin (or $CS_ORCH_TRANSCRIPT) if available;
  otherwise no-ops cleanly (hooks must never hard-fail the session).
- Extracts material numeric tokens from the answer and checks each appears in the
  known evidence set (all numbers present in the fixtures).
- Ungrounded numbers -> emits a WARNING annotation (non-blocking) so the reader
  knows exactly which figures to distrust. Exit 0 always (advisory gate).

This is deliberately dependency-free and side-effect-light so it is safe to run on
every Stop event.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

NUMBER = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")
FIXTURES = os.environ.get(
    "CS_FIXTURES",
    str(Path(__file__).resolve().parents[2] / "mcp-servers" / "fixtures" / "accounts.json"),
)


def _norm(tok: str) -> str:
    tok = tok.strip().lstrip("$").rstrip("%").replace(",", "")
    if "." in tok:
        whole, frac = tok.split(".", 1)
        frac = frac.rstrip("0")
        tok = whole if not frac else f"{whole}.{frac}"
    return tok


def _trivial(norm: str) -> bool:
    # Small integers appear incidentally in prose ("top 3", "priority 1").
    return norm in {str(i) for i in range(0, 13)} or norm == ""


def _evidence_numbers() -> set[str]:
    """Every number that appears anywhere in the fixtures = the grounded set.

    Also expands churn scores to their percentage form (0.72 -> 72) since the
    agent will often phrase a 0.72 score as '72%'."""
    try:
        raw = Path(FIXTURES).read_text(encoding="utf-8")
    except OSError:
        return set()
    nums: set[str] = {_norm(t) for t in NUMBER.findall(raw)}
    expanded: set[str] = set(nums)
    for n in list(nums):
        try:
            f = float(n)
        except ValueError:
            continue
        if 0 < f < 1:  # churn score -> percentage
            expanded.add(_norm(str(round(f * 100))))
    return expanded


def _read_answer() -> str:
    path = os.environ.get("CS_ORCH_TRANSCRIPT")
    if path and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    data = sys.stdin.read() if not sys.stdin.isatty() else ""
    return data


def main() -> int:
    answer = _read_answer()
    if not answer.strip():
        return 0  # nothing to check; never block

    evidence = _evidence_numbers()
    if not evidence:
        return 0

    seen: set[str] = set()
    ungrounded: list[str] = []
    for tok in NUMBER.findall(answer):
        norm = _norm(tok)
        if _trivial(norm) or norm in evidence or tok in seen:
            continue
        seen.add(tok)
        ungrounded.append(tok)

    if ungrounded:
        sys.stderr.write(
            "[cs-orchestrator grounding-gate] WARNING: figures not found in CS "
            "evidence (verify before acting): " + ", ".join(ungrounded) + "\n"
        )
    else:
        sys.stderr.write("[cs-orchestrator grounding-gate] OK: all figures grounded in CS evidence.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
