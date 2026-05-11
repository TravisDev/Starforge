# tools/

The tool registry. One subdirectory per available tool; each carries a
`manifest.yaml` describing what the tool does, what privileges it needs, and
its review status.

## Why this directory exists

A tool, in Starforge's model, is something the agent's LLM can invoke during a
run — `http_get`, `set_task_status`, `add_comment`, etc. Tools have full
process privileges of the nemoclaw container they run in (no sandboxing yet —
see roadmap below). That means a malicious tool can do arbitrary harm.

So tools need **gate-keeping**. The registry is that gate:

- Each tool has a `manifest.yaml` describing its capabilities and review status
- Only tools whose manifest says `status: approved` can be used by agents
- An admin must explicitly Approve a tool before it goes live
- Today the built-in tools live in `nemoclaw/runner.py` as Python functions —
  the registry serves as **documentation + the approval surface**. The PR
  review of the implementation IS the security review.
- When we move to externally-contributed tools (Phase D.2 below), the registry
  also pins the SHA-256 of the tool's source file. Any post-approval mutation
  invalidates approval — the runtime refuses to load it until re-reviewed.

## Manifest schema

```yaml
version: 1
tool:
  slug: my-tool
  name: My Tool
  description: "What it does."
  builtin: true                # true → implemented in nemoclaw/runner.py;
                                # false → dynamic-load from this dir (Phase D.2)
  status: approved             # approved | draft | rejected
  approved_at: "2026-..."      # set when an admin approves
  approved_by: "..."           # admin email / handle
  code_sha256: "..."           # pinned when status=approved and builtin=false
  
  capabilities:
    network:
      egress: []               # list of CIDRs / hostnames / "any" / "starforge-internal"
    filesystem:
      read: []                 # absolute or relative paths
      write: []
    env_vars: []               # which env vars the tool reads
  
  inputs:                      # what the LLM provides when calling the tool
    - name: ...
      type: string | enum | object | hostname | url | duration
      required: true
      description: "..."
```

## Adding a new tool

1. Create `./tools/<slug>/manifest.yaml` with `status: draft`
2. For dynamic-load (non-builtin) tools: also drop `tool.py` in the same dir
3. An admin reviews the manifest + source via Settings → Tool drafts
4. Admin clicks Approve → status flips, SHA-256 gets pinned

The same admin-review UI that gates new agent types (Settings → Agent type
drafts) gates tool drafts. Same trust pattern.

## Roadmap

| Phase | Scope |
|---|---|
| ✅ D.1 (this) | Registry schema + manifests for all current builtins, admin approve/reject UI, list endpoint. Built-ins land pre-approved. |
| 🟡 D.2 | Dynamic loading of non-builtin tools from `./tools/<slug>/tool.py`. SHA-256 verification at load time — mismatch refuses to load. |
| 🟡 D.3 | agent-builder validates tool references against the registry — refuses to draft agents that reference unknown / unapproved tools. |
| 🟡 D.4 | Real sandboxing — per-tool egress allowlists enforced at the network layer (iptables / Docker network policies), filesystem read/write restrictions, resource limits. Until this lands, "approved" still means "trusted process privileges." |
