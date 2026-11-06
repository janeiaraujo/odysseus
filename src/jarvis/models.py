"""Jarvis tables. `owner` is the Odysseus username; "" means single-user mode."""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from src.jarvis.db import Base, utcnow


def new_id() -> str:
    return uuid.uuid4().hex


class AuditEvent(Base):
    __tablename__ = "jarvis_audit"
    id = Column(String, primary_key=True, default=new_id)
    ts = Column(DateTime, default=utcnow, index=True)
    owner = Column(String, default="", index=True)
    actor = Column(String, default="jarvis")
    action = Column(String, nullable=False, index=True)
    target = Column(String, default="")
    risk = Column(String, default="read")
    outcome = Column(String, default="ok")
    detail = Column(Text, default="{}")


class Approval(Base):
    __tablename__ = "jarvis_approvals"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    action = Column(String, nullable=False)
    risk = Column(String, nullable=False)
    summary = Column(Text, default="")
    payload_enc = Column(Text, default="")
    status = Column(String, default="pending", index=True)  # pending|approved|rejected|expired|executed|failed
    result = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)
    expires_at = Column(DateTime, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decided_by = Column(String, default="")


class ModelUsage(Base):
    __tablename__ = "jarvis_model_usage"
    id = Column(String, primary_key=True, default=new_id)
    ts = Column(DateTime, default=utcnow, index=True)
    owner = Column(String, default="", index=True)
    provider = Column(String, default="unknown", index=True)
    model = Column(String, default="", index=True)
    tier = Column(String, default="unknown")
    purpose = Column(String, default="chat")
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    latency_ms = Column(Integer, default=0)
    ok = Column(Boolean, default=True)
    error_kind = Column(String, default="")
    cost_usd = Column(Float, nullable=True)  # NULL = price unknown, never guessed


class MemoryItem(Base):
    __tablename__ = "jarvis_memory"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    kind = Column(String, default="semantic", index=True)  # episodic|semantic|procedural|operational
    category = Column(String, default="fact", index=True)
    text = Column(Text, nullable=False)
    norm_hash = Column(String, index=True)
    source_type = Column(String, default="user")  # user|assistant|document|email|web|system|import
    source_ref = Column(String, default="")
    trust = Column(String, default="user")  # user|derived|untrusted
    status = Column(String, default="fact", index=True)  # fact|hypothesis|conflicted|superseded
    confidence = Column(Float, default=1.0)
    priority = Column(Integer, default=0)
    valid_until = Column(DateTime, nullable=True)
    version = Column(Integer, default=1)
    uses = Column(Integer, default=0)
    last_used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)
    deleted_at = Column(DateTime, nullable=True, index=True)
    __table_args__ = (Index("ix_jarvis_memory_owner_hash", "owner", "norm_hash"),)


class MemoryVersion(Base):
    __tablename__ = "jarvis_memory_versions"
    id = Column(String, primary_key=True, default=new_id)
    memory_id = Column(String, ForeignKey("jarvis_memory.id", ondelete="CASCADE"), index=True)
    version = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    reason = Column(String, default="")
    changed_at = Column(DateTime, default=utcnow)


class MemoryLink(Base):
    __tablename__ = "jarvis_memory_links"
    id = Column(String, primary_key=True, default=new_id)
    src_id = Column(String, ForeignKey("jarvis_memory.id", ondelete="CASCADE"), index=True)
    dst_id = Column(String, ForeignKey("jarvis_memory.id", ondelete="CASCADE"), index=True)
    relation = Column(String, default="related")  # related|conflicts|corrects|consolidates
    created_at = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("src_id", "dst_id", "relation"),)


class LearningCandidate(Base):
    __tablename__ = "jarvis_learning_candidates"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    text = Column(Text, nullable=False)
    kind = Column(String, default="semantic")
    category = Column(String, default="fact")
    signal = Column(String, default="")
    source_type = Column(String, default="user")
    source_ref = Column(String, default="")
    confidence = Column(Float, default=0.5)
    status = Column(String, default="pending", index=True)  # pending|accepted|rejected
    memory_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    decided_at = Column(DateTime, nullable=True)


class TrainingExample(Base):
    __tablename__ = "jarvis_training_examples"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    prompt = Column(Text, nullable=False)
    completion = Column(Text, nullable=False)
    norm_hash = Column(String, index=True)
    validated = Column(Boolean, default=False, index=True)
    source = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("owner", "norm_hash"),)


class DatasetVersion(Base):
    __tablename__ = "jarvis_dataset_versions"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="")
    version = Column(Integer, nullable=False)
    path = Column(String, nullable=False)
    sha256 = Column(String, nullable=False)
    examples = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)


class TrainingRun(Base):
    __tablename__ = "jarvis_training_runs"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="")
    dataset_id = Column(String, ForeignKey("jarvis_dataset_versions.id", ondelete="SET NULL"), nullable=True)
    base_model = Column(String, default="")
    method = Column(String, default="lora")
    status = Column(String, default="planned")  # planned|running|succeeded|failed|active|rolled_back
    output_ref = Column(String, default="")
    metrics = Column(Text, default="{}")
    created_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)


class Mission(Base):
    __tablename__ = "jarvis_missions"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    title = Column(String, nullable=False)
    goal = Column(Text, default="")
    status = Column(String, default="active", index=True)  # active|completed|failed|cancelled|waiting_approval
    cron = Column(String, nullable=True)
    timezone = Column(String, default="America/Sao_Paulo")
    next_run_at = Column(DateTime, nullable=True, index=True)
    run_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


class MissionStep(Base):
    __tablename__ = "jarvis_mission_steps"
    id = Column(String, primary_key=True, default=new_id)
    mission_id = Column(String, ForeignKey("jarvis_missions.id", ondelete="CASCADE"), index=True)
    idx = Column(Integer, default=0)
    title = Column(String, default="")
    handler = Column(String, nullable=False)
    params = Column(Text, default="{}")
    depends_on = Column(Text, default="[]")  # JSON list of step idx
    status = Column(String, default="pending", index=True)  # pending|running|succeeded|failed|cancelled|waiting_approval
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    retry_at = Column(DateTime, nullable=True)
    lease_until = Column(DateTime, nullable=True)
    lease_owner = Column(String, default="")
    approval_id = Column(String, nullable=True)
    result = Column(Text, default="")
    error = Column(Text, default="")
    updated_at = Column(DateTime, default=utcnow)


class OAuthAccount(Base):
    __tablename__ = "jarvis_oauth_accounts"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    provider = Column(String, default="google")
    subject = Column(String, default="")
    email = Column(String, default="")
    scopes = Column(Text, default="")
    access_enc = Column(Text, default="")
    refresh_enc = Column(Text, default="")
    expires_at = Column(DateTime, nullable=True)
    status = Column(String, default="connected")  # connected|reauth_required|revoked
    last_error = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("owner", "provider", "subject"),)


class OAuthState(Base):
    __tablename__ = "jarvis_oauth_states"
    state_hash = Column(String, primary_key=True)
    owner = Column(String, default="")
    verifier_enc = Column(Text, default="")
    scopes = Column(Text, default="")
    purpose = Column(String, default="connect")
    expires_at = Column(DateTime, nullable=False)


class OutboundEmail(Base):
    __tablename__ = "jarvis_outbound_email"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    account_id = Column(String, default="")
    idempotency_key = Column(String, nullable=False, unique=True)
    payload_enc = Column(Text, nullable=False)  # recipients/subject/body are personal data
    send_at = Column(DateTime, nullable=False, index=True)
    timezone = Column(String, default="America/Sao_Paulo")
    status = Column(String, default="queued", index=True)  # awaiting_approval|queued|sending|sent|cancelled|failed|uncertain
    approval_id = Column(String, nullable=True)
    attempts = Column(Integer, default=0)
    lease_until = Column(DateTime, nullable=True)
    provider_message_id = Column(String, default="")
    error = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)
    sent_at = Column(DateTime, nullable=True)


class EmailInsight(Base):
    __tablename__ = "jarvis_email_insights"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    account_id = Column(String, default="")
    message_id = Column(String, nullable=False)
    thread_id = Column(String, default="")
    category = Column(String, default="")
    important = Column(Boolean, default=False)
    suspected_spam = Column(Boolean, default=False)
    action_items = Column(Text, default="[]")
    created_at = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("owner", "account_id", "message_id"),)


class SyncState(Base):
    __tablename__ = "jarvis_sync_state"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    provider = Column(String, nullable=False)
    resource = Column(String, nullable=False)
    cursor = Column(Text, default="")
    last_ok_at = Column(DateTime, nullable=True)
    last_error = Column(String, default="")
    updated_at = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("owner", "provider", "resource"),)


class IndexedFile(Base):
    __tablename__ = "jarvis_indexed_files"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    provider = Column(String, default="gdrive")
    file_id = Column(String, nullable=False)
    name = Column(String, default="")
    md5 = Column(String, default="")
    modified_time = Column(String, default="")
    allowed = Column(Boolean, default=True)
    indexed_at = Column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("owner", "provider", "file_id"),)


class Device(Base):
    __tablename__ = "jarvis_devices"
    id = Column(String, primary_key=True, default=new_id)
    owner = Column(String, default="", index=True)
    name = Column(String, nullable=False)
    aliases = Column(Text, default="[]")
    domain = Column(String, default="")  # light|switch|climate|media_player|sensor|camera|lock|cover|...
    entity_id = Column(String, nullable=False)
    area = Column(String, default="")
    source = Column(String, default="manual")
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("owner", "entity_id"),)


class ObjectIndex(Base):
    __tablename__ = "jarvis_object_index"
    key = Column(String, primary_key=True)
    owner = Column(String, default="", index=True)
    backend = Column(String, default="local")
    namespace = Column(String, default="", index=True)
    size = Column(Integer, default=0)
    sha256 = Column(String, default="")
    meta_enc = Column(Text, default="")
    created_at = Column(DateTime, default=utcnow)
    deleted_at = Column(DateTime, nullable=True)


class StorageOp(Base):
    __tablename__ = "jarvis_storage_ops"
    id = Column(String, primary_key=True, default=new_id)
    ts = Column(DateTime, default=utcnow, index=True)
    backend = Column(String, default="")
    op_class = Column(String, default="B")  # A = writes/lists, B = reads (R2 billing classes)
    op = Column(String, default="")
    bytes = Column(Integer, default=0)
    ok = Column(Boolean, default=True)


class ReplicationOutbox(Base):
    __tablename__ = "jarvis_replication_outbox"
    id = Column(String, primary_key=True, default=new_id)
    target = Column(String, default="firestore", index=True)
    doc_path = Column(String, nullable=False)
    op = Column(String, default="upsert")
    payload = Column(Text, default="{}")
    attempts = Column(Integer, default=0)
    last_error = Column(String, default="")
    created_at = Column(DateTime, default=utcnow)
    done_at = Column(DateTime, nullable=True, index=True)
