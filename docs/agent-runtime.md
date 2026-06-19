# Agent Runtime Architecture

This document is the stable contract for turning the bridge from a Codex-only
Telegram bridge into a small control plane for terminal AI agent heads.

## Goals

- Keep Codex REPL as the first-class default head.
- Allow Claude Code, Gemini CLI, GLM, Aider, and generic command heads to be
  added without changing Telegram transport logic.
- Prevent stale approval buttons and cross-head workdir conflicts.
- Keep legacy `~/.claude` and `claude-automations` paths as forwarders, not the
  new source of truth.

## Head Adapter Contract

Each head implements:

```python
spawn() -> None
send(message: AgentMessage) -> None
recv() -> Iterable[AgentEvent]
inject_approval(choice: ApprovalOption | str) -> None
kill() -> None
capabilities() -> HeadCapabilities
```

`send()` accepts structured `AgentMessage` values so future heads can receive
text plus media paths. `recv()` emits structured `AgentEvent` values; REPL heads
may implement it by tailing their native JSONL/session stream.

## Approval Model

Every approval prompt is represented by `ApprovalRequest`:

- `approval_id`
- `expires_at`
- `cancelled`
- `source_head`
- `command`
- `reason`
- `options`

Telegram button callback data must include the short approval id. The control
plane must reject callbacks when:

- there is no active approval request
- the request id does not match the current request
- the request is expired
- the request was cancelled or already resolved

Default TTL is five minutes unless a head explicitly configures a shorter value.

## Capability Registry

Heads declare `HeadCapabilities` instead of relying on a generic exec fallback:

- `vision`
- `audio`
- `video`
- `repl`
- `approval`
- `streaming`
- `workdir_access`

Routing decisions should use the registry. For example, image prompts should
prefer heads with `vision=True`; command approval UI should only appear for heads
with `approval=True`.

## Transport Boundary

Local input mechanisms must sit behind a `Transport` interface:

- `QueueTransport` for tests and in-process control
- `FifoTransport` for the current local input path
- future Unix socket or HTTP transports without bridge changes

The Telegram layer should never directly depend on FIFO details.

## Workdir Isolation

Before a head mutates a repo, the control plane should acquire a `WorkdirLock`.
If multiple heads need the same repo simultaneously, prefer per-head worktrees.
The lock file records owner, pid, workdir, and created time so dead holders can
be cleaned up.

## Legacy Cutoff

New installs should use this repo and the `agent_runtime` package as source of
truth. Existing `~/.claude` and `claude-automations` deployments may forward to
the new runtime during migration, but new features should land here first and be
copied to live legacy scripts only as a temporary shim.

Cutoff rule:

- new user-facing docs point to `codex-telegram-bridge`
- legacy paths are compatibility shims
- after the multi-head runtime owns Codex REPL in production, remove feature
  development from legacy-only scripts
