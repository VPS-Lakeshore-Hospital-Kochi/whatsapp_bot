"""Run the eval question set against the real model and the given knowledge folder.

Uses real Claude calls (costs money; roughly two calls per 'answered' question). Nothing is
sent on WhatsApp. Prints a table and exits non-zero if any case fails, so it can gate CI.
"""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

import yaml

from app.answer import LLM, Answerer
from app.audit import Audit
from app.config import get_settings
from app.his.mock import MockHIS
from app.identity import Identity, StaffDirectory
from app.knowledge.loader import load_knowledge
from app.knowledge.retriever import Retriever
from app.router import Router
from app.store import MemoryStore
from app.whatsapp.client import RecordingSender
from app.whatsapp.parse import Inbound


def classify(body: str, emergency_phone: str) -> str:
    if f"call *{emergency_phone}* now" in body:
        return "emergency"
    if "ref #" in body:
        return "handoff"
    if "won't guess" in body:
        return "declined"
    if "_Source:" in body:
        return "answered"
    return "other"


async def main(knowledge: Path, cases_file: Path) -> int:
    settings = get_settings().model_copy(update={"knowledge_dir": knowledge})
    cases = yaml.safe_load(cases_file.read_text())
    answerer = Answerer(Retriever(load_knowledge(knowledge)), LLM(settings), settings)
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, case in enumerate(cases):
            store, sender = MemoryStore(), RecordingSender()
            wa = f"9100000{i:05d}"
            await store.set(f"consent:{wa}", "eval")
            if case["role"] != "patient":
                await store.set(f"auth:{wa}", case["role"])
            router = Router(settings, store, sender, Identity(StaffDirectory([]), store, 1, 3, 1),
                            answerer, MockHIS(), Audit(f"sqlite:///{tmp}/eval.db", "eval"))
            await router.handle(Inbound(message_id=f"e{i}", wa_id=wa, kind="text", text=case["question"]))
            body = sender.last_body()
            got = classify(body, settings.emergency_phone)
            ok = got == case["expect"]
            ok &= all(s in body for s in case.get("must_include", []))
            ok &= not any(s in body for s in case.get("must_not_include", []))
            failures += not ok
            print(f"{'PASS' if ok else 'FAIL'}  {case['id']:<22} expected={case['expect']:<10} got={got}")
            if not ok:
                print("      reply:", body.replace("\n", " ")[:300])
    print(f"\n{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--knowledge", type=Path, default=Path("knowledge"))
    p.add_argument("--cases", type=Path, default=Path("evals/questions.yaml"))
    args = p.parse_args()
    sys.exit(asyncio.run(main(args.knowledge, args.cases)))
