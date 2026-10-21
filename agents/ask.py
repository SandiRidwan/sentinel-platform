"""Ask the warehouse agent a question.

    python -m agents.ask "how many books are out of stock?"
    python -m agents.ask --json "which country has the largest population?"

Prints the answer, and with --json the plan, the SQL and the rows, so the answer can be
checked instead of believed.
"""

from __future__ import annotations

import argparse
import json

from agents.graph import Agent
from tools.common.logging_setup import setup_logging


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Ask a question about the gold layer.")
    parser.add_argument("question", nargs="+", help="the question, in quotes")
    parser.add_argument("--json", action="store_true", help="print the full trace of the answer")
    args = parser.parse_args(argv)

    question = " ".join(args.question)
    agent = Agent()
    try:
        answer = agent.ask(question)
    finally:
        agent.close()

    if args.json:
        print(json.dumps(answer, indent=2, default=str))
    else:
        print(answer["answer"])
        if answer["refused"]:
            print(f"(refused: {answer['refusal_reason']})")
        elif answer.get("sql"):
            print(f"\nsql: {answer['sql']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
