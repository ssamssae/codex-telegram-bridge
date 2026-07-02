# Codex Runtime Architecture

This document is the stable contract for Codex Telegram Bridge runtime pieces.
The project supports Codex only. Other AI CLIs should live in separate
bridge-specific projects, not as shared modes in this repository. Shared runtime
modules exist to keep Codex REPL sync, approval handling, transport, and workdir
locking separated.

## Goals

- Keep Codex REPL as the first-class default path.
- Keep text-only `codex exec` mode as the lightweight fallback path.
- Prevent stale approval buttons and overlapping Codex workdir mutations.
- Keep setup, service files, docs, and diagnostics focused on Codex behavior.
- Preserve legacy private-deployment paths as compatibility shims where
  needed, not as the source of truth.

## Codex Adapter Contract

The Codex REPL adapter owns the visible tmux Codex session and its JSONL stream:

```python
spawn() -> None
send(message: AgentMessage) -> None
recv() -> Iterable[AgentEvent]
inject_approval(choice: ApprovalOption | str) -> None
kill() -> None
capabilities() -> HeadCapabilities
```

`send()` accepts structured `AgentMessage` values so Telegram text and local
media paths stay explicit. `recv()` emits structured `AgentEvent` values from
Codex JSONL/session state.

## Approval Model

Every Codex approval prompt is represented by `ApprovalRequest`:

- `approval_id`
- `expires_at`
- `cancelled`
- `source_head`
- `command`
- `reason`
- `options`

Telegram button callback data must include the short approval id. The bridge
rejects callbacks when:

- there is no active approval request
- the request id does not match the current request
- the request is expired
- the request was cancelled or already resolved

Default TTL is five minutes unless the Codex path configures a shorter value.

## Capability Registry

Capabilities describe the Codex path currently running:

- `vision`
- `audio`
- `video`
- `repl`
- `approval`
- `streaming`
- `workdir_access`

Routing decisions should use the registry for Codex features. For example,
image prompts require `vision=True`, and approval UI requires `approval=True`.

## Transport Boundary

Local input mechanisms sit behind a `Transport` interface:

- `QueueTransport` for tests and in-process control
- `FifoTransport` for the current local input path

The Telegram layer should not directly depend on FIFO details.

## Workdir Isolation

Before Codex mutates a repo, the bridge should acquire a `WorkdirLock`. The lock
file records owner, pid, workdir, and created time so dead holders can be
cleaned up. If separate Codex sessions need the same repo simultaneously, use
separate worktrees rather than bypassing the lock.

## Legacy Cutoff

New installs should use this repo as the source of truth. Existing private
legacy deployments may forward to the new runtime during migration, but new
Codex Telegram Bridge features should land here first and be copied to live
legacy scripts only as temporary shims.

Cutoff rule:

- new user-facing docs point to `codex-telegram-bridge`
- legacy paths are compatibility shims
- new bridge behavior stays scoped to Codex
