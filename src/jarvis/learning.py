"""Jarvis Learning Engine.

Extracts durable knowledge from user messages with deterministic rules, so it
costs no tokens and is testable. Rules:
- Only the USER's own words can become facts, and only explicit commands are
  accepted automatically. Everything else waits in a review queue.
- Questions, hypotheticals and assistant text never become facts.
- Text from email/web/documents is a hypothesis, and imperative or
  instruction-like lines are discarded rather than learned.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

from src.constants import JARVIS_DATASETS_DIR
from src.jarvis import audit, config, memory
from src.jarvis.db import session_scope, utcnow
from src.jarvis.models import DatasetVersion, LearningCandidate, TrainingExample, TrainingRun
from src.jarvis.redact import redact_text

_NAME = r"(?:jarvis[,:!]?\s+)?"
_EXPLICIT = re.compile(rf"^\s*{_NAME}(?:por favor,?\s+)?(?:lembre(?:-se)?|lembra|memorize|guarde|grave|anote|remember)\s+(?:que|de que|disso|that|:)\s*[:,]?\s*(.+)$", re.I | re.S)
_FORGET = re.compile(rf"^\s*{_NAME}(?:por favor,?\s+)?(?:esque[cç]a|forget)\s+(?:que|de que|that|sobre|about)?\s*(.+)$", re.I | re.S)
_CORRECTION = re.compile(r"^\s*(?:na verdade|corrigindo|correcao|correção|actually)[,:]?\s+(.+)$", re.I | re.S)
_RULES = [
    ("preference", re.compile(r"\b(?:eu\s+)?(?:prefiro|gosto de|nao gosto de|não gosto de|odeio|adoro|i prefer|i like|i hate)\s+[^.!?\n]{3,160}", re.I)),
    ("identity", re.compile(r"\b(?:meu nome [eé]|me chamo|moro em|vivo em|trabalho (?:na|no|em|como)|sou (?:formado|casado|solteiro)|my name is|i live in|i work (?:at|as|in))\s+[^.!?\n]{2,120}", re.I)),
    ("fact", re.compile(r"\b(?:meu|minha)\s+[\wãáéíóúçõâêô ]{2,40}\s+(?:[eé]|fica)\s+[^.!?\n]{2,120}", re.I)),
]
_HYPOTHETICAL = re.compile(r"\b(talvez|acho que|se eu|caso eu|sera que|será que|maybe|i think|what if|imagine)\b", re.I)
_REQUEST = re.compile(r"\b(voce pode|você pode|pode me|me ajude|por favor|poderia|could you|can you|please)\b", re.I)
_INSTRUCTION_LIKE = re.compile(
    r"(ignore|desconsidere|esque[cç]a)\s+.*(instru|regras|prompt|anteriores)|(^|\b)(system|sistema)\s*:|prompt|"
    r"\b(voce deve|você deve|you must|execute|encaminhe|envie|transfira|send|forward|run|delete|apague|ignore)\b", re.I)
_MAX_LEN = 300


def extract(text: str) -> Dict[str, Any]:
    """Pure function: what does this user message teach? Returns explicit/forget/correction/candidates."""
    text = (text or "").strip()
    out: Dict[str, Any] = {"explicit": None, "forget": None, "correction": None, "candidates": []}
    if not text or len(text) > 4000 or text.startswith("/"):
        return out
    match = _EXPLICIT.match(text)
    if match:
        out["explicit"] = match.group(1).strip().rstrip(".")[:_MAX_LEN]
        return out
    match = _FORGET.match(text)
    if match:
        out["forget"] = match.group(1).strip().rstrip(".")[:_MAX_LEN]
        return out
    if text.endswith("?") or _REQUEST.search(text):
        return out
    match = _CORRECTION.match(text)
    if match:
        out["correction"] = match.group(1).strip().rstrip(".")[:_MAX_LEN]
        return out
    hypothetical = bool(_HYPOTHETICAL.search(text))
    seen = set()
    for category, pattern in _RULES:
        for hit in pattern.finditer(text):
            value = hit.group(0).strip().rstrip(".,")
            if value.lower() in seen:
                continue
            seen.add(value.lower())
            out["candidates"].append({"text": value[:_MAX_LEN], "category": category, "signal": "hypothetical" if hypothetical else "statement",
                                      "confidence": 0.35 if hypothetical else 0.7})
    return out


def _add_candidate(owner: str, text: str, category: str, signal: str, confidence: float, source_type: str, source_ref: str) -> Optional[str]:
    with session_scope() as db:
        duplicate = db.query(LearningCandidate.id).filter(LearningCandidate.owner == owner, LearningCandidate.text == text,
                                                          LearningCandidate.status == "pending").first()
        if duplicate:
            return None
        row = LearningCandidate(owner=owner, text=text, category=category, signal=signal, confidence=confidence,
                                source_type=source_type, source_ref=redact_text(source_ref)[:300])
        db.add(row)
        db.flush()
        return row.id


def observe_user_message(text: str, *, owner: Optional[str] = "", source_ref: str = "") -> Dict[str, Any]:
    """Process one authorized user message. Safe to call after every turn."""
    owner = owner or ""
    result: Dict[str, Any] = {"remembered": [], "candidates": [], "forgotten": []}
    if not config.get("learning.enabled", True):
        return result
    found = extract(text)
    if found["explicit"]:
        if config.get("learning.auto_accept_explicit", True):
            result["remembered"].append(memory.remember(found["explicit"], owner=owner, source_type="user", source_ref=source_ref))
        else:
            cid = _add_candidate(owner, found["explicit"], "fact", "explicit", 0.9, "user", source_ref)
            result["candidates"] += [cid] if cid else []
    if found["forget"]:
        hits = memory.search(found["forget"], owner=owner, k=1, include_hypotheses=True)
        if hits and memory.similarity(hits[0]["text"], found["forget"]) >= 0.4:
            memory.delete(hits[0]["id"], owner=owner)
            result["forgotten"].append(hits[0]["id"])
    if found["correction"]:
        cid = _add_candidate(owner, found["correction"], "fact", "correction", 0.8, "user", source_ref)
        result["candidates"] += [cid] if cid else []
    for cand in found["candidates"]:
        cid = _add_candidate(owner, cand["text"], cand["category"], cand["signal"], cand["confidence"], "user", source_ref)
        result["candidates"] += [cid] if cid else []
    return result


def observe_untrusted(text: str, *, source_type: str, source_ref: str = "", owner: Optional[str] = "") -> List[str]:
    """Queue facts from external content. Instruction-like lines are dropped, never stored."""
    owner = owner or ""
    if source_type not in memory.UNTRUSTED_SOURCES or not config.get("learning.enabled", True):
        return []
    ids: List[str] = []
    for line in re.split(r"[\n.!?]+", text or ""):
        line = line.strip()
        if len(line) < 8 or _INSTRUCTION_LIKE.search(line):
            continue
        for category, pattern in _RULES:
            hit = pattern.search(line)
            if hit:
                cid = _add_candidate(owner, hit.group(0).strip()[:_MAX_LEN], category, "untrusted", 0.3, source_type, source_ref)
                if cid:
                    ids.append(cid)
                break
    return ids


def list_candidates(owner: Optional[str] = "", status: str = "pending", limit: int = 100) -> List[Dict[str, Any]]:
    with session_scope() as db:
        q = db.query(LearningCandidate).filter(LearningCandidate.status == status)
        if owner is not None:
            q = q.filter(LearningCandidate.owner == (owner or ""))
        return [{"id": r.id, "text": r.text, "category": r.category, "signal": r.signal, "source_type": r.source_type,
                 "confidence": r.confidence, "created_at": r.created_at.isoformat() + "Z"}
                for r in q.order_by(LearningCandidate.created_at.desc()).limit(limit).all()]


def decide_candidate(candidate_id: str, accept: bool, *, owner: Optional[str] = "", edited_text: Optional[str] = None) -> Dict[str, Any]:
    with session_scope() as db:
        claimed = db.query(LearningCandidate).filter(LearningCandidate.id == candidate_id, LearningCandidate.owner == (owner or ""),
                                                     LearningCandidate.status == "pending").update(
            {"status": "accepted" if accept else "rejected", "decided_at": utcnow()}, synchronize_session=False)
        if not claimed:
            raise LookupError("candidato inexistente ou ja decidido")
        row = db.get(LearningCandidate, candidate_id)
        text, category, signal, source_type, source_ref, confidence = row.text, row.category, row.signal, row.source_type, row.source_ref, row.confidence
    if not accept:
        return {"status": "rejected"}
    final = (edited_text or text).strip()
    if signal == "correction":  # replace what the user was correcting, keeping its history
        hits = memory.search(final, owner=owner, k=1, include_hypotheses=True)
        if hits and memory.similarity(hits[0]["text"], final) >= 0.3:
            item = memory.correct(hits[0]["id"], final, owner=owner, reason="correcao aprendida em conversa")
            add_training_example("correcao", final, owner=owner, source="correction")
            return {"status": "accepted", "result": {"action": "corrected", "item": item}}
    # A human confirmed it, so an originally untrusted text is now user knowledge.
    result = memory.remember(final, owner=owner, category=category, source_type="user",
                             source_ref=f"candidate:{candidate_id}|{source_type}|{source_ref}"[:300], confidence=max(confidence, 0.9))
    with session_scope() as db:
        db.get(LearningCandidate, candidate_id).memory_id = result["item"]["id"]
    return {"status": "accepted", "result": result}


# --- datasets for optional fine-tuning. Weights never change per interaction. ---

def add_training_example(prompt: str, completion: str, *, owner: Optional[str] = "", validated: bool = False, source: str = "") -> Optional[str]:
    digest = hashlib.sha256(f"{memory.normalize(prompt)}\0{memory.normalize(completion)}".encode()).hexdigest()
    with session_scope() as db:
        if db.query(TrainingExample.id).filter(TrainingExample.owner == (owner or ""), TrainingExample.norm_hash == digest).first():
            return None
        row = TrainingExample(owner=owner or "", prompt=prompt, completion=completion, norm_hash=digest, validated=validated, source=source)
        db.add(row)
        db.flush()
        return row.id


def validate_example(example_id: str, validated: bool, owner: Optional[str] = "") -> None:
    with session_scope() as db:
        updated = db.query(TrainingExample).filter(TrainingExample.id == example_id, TrainingExample.owner == (owner or "")).update({"validated": validated})
        if not updated:
            raise LookupError("exemplo nao encontrado")


def export_dataset(owner: Optional[str] = "") -> Dict[str, Any]:
    """Write validated examples to a versioned JSONL file, scrubbed of secrets."""
    with session_scope() as db:
        rows = db.query(TrainingExample).filter(TrainingExample.owner == (owner or ""), TrainingExample.validated == True).order_by(TrainingExample.created_at).all()  # noqa: E712
        lines = [json.dumps({"messages": [{"role": "user", "content": redact_text(r.prompt)}, {"role": "assistant", "content": redact_text(r.completion)}]}, ensure_ascii=False) for r in rows]
        if not lines:
            raise ValueError("nenhum exemplo validado para exportar")
        version = (db.query(DatasetVersion).filter(DatasetVersion.owner == (owner or "")).count()) + 1
    body = "\n".join(lines) + "\n"
    digest = hashlib.sha256(body.encode()).hexdigest()
    os.makedirs(JARVIS_DATASETS_DIR, exist_ok=True)
    path = os.path.join(JARVIS_DATASETS_DIR, f"dataset-{hashlib.sha256((owner or '').encode()).hexdigest()[:8]}-v{version}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o600)
    with session_scope() as db:
        row = DatasetVersion(owner=owner or "", version=version, path=path, sha256=digest, examples=len(lines))
        db.add(row)
        db.flush()
        dataset_id = row.id
    audit.record("learning.dataset_export", owner=owner, risk="reversible", version=version, examples=len(lines))
    return {"id": dataset_id, "version": version, "examples": len(lines), "sha256": digest, "path": path}


def plan_training(dataset_id: str, base_model: str, owner: Optional[str] = "", method: str = "lora") -> Dict[str, Any]:
    """Register a training job and return the command to run it on a machine with a GPU."""
    with session_scope() as db:
        dataset = db.query(DatasetVersion).filter(DatasetVersion.id == dataset_id, DatasetVersion.owner == (owner or "")).first()
        if dataset is None:
            raise LookupError("dataset nao encontrado")
        run = TrainingRun(owner=owner or "", dataset_id=dataset_id, base_model=base_model, method=method)
        db.add(run)
        db.flush()
        return {"id": run.id, "status": run.status, "dataset_sha256": dataset.sha256,
                "command": f"python scripts/jarvis-train --run {run.id} --dataset {dataset.path} --base-model {base_model} --method {method}"}


def record_training_result(run_id: str, *, status: str, output_ref: str = "", metrics: Optional[Dict[str, Any]] = None, owner: Optional[str] = "") -> None:
    if status not in ("running", "succeeded", "failed"):
        raise ValueError("status invalido")
    with session_scope() as db:
        run = db.query(TrainingRun).filter(TrainingRun.id == run_id, TrainingRun.owner == (owner or "")).first()
        if run is None:
            raise LookupError("treinamento nao encontrado")
        run.status, run.output_ref, run.metrics = status, output_ref, json.dumps(metrics or {})
        if status != "running":
            run.finished_at = utcnow()


def activate_training(run_id: str, owner: Optional[str] = "") -> None:
    """Mark a succeeded model as active; the previous one stays available for rollback."""
    with session_scope() as db:
        run = db.query(TrainingRun).filter(TrainingRun.id == run_id, TrainingRun.owner == (owner or ""), TrainingRun.status == "succeeded").first()
        if run is None:
            raise LookupError("apenas treinamentos concluidos podem ser ativados")
        db.query(TrainingRun).filter(TrainingRun.owner == (owner or ""), TrainingRun.status == "active").update({"status": "succeeded"})
        run.status = "active"


def rollback_training(owner: Optional[str] = "") -> Optional[str]:
    """Deactivate the active model, returning to the most recent earlier success."""
    with session_scope() as db:
        active = db.query(TrainingRun).filter(TrainingRun.owner == (owner or ""), TrainingRun.status == "active").first()
        if active is None:
            return None
        active.status = "rolled_back"
        previous = db.query(TrainingRun).filter(TrainingRun.owner == (owner or ""), TrainingRun.status == "succeeded").order_by(TrainingRun.finished_at.desc()).first()
        if previous is None:
            return None
        previous.status = "active"
        return previous.id
