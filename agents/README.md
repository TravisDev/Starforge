# agents/

AI-agent configuration lives here. Each subdirectory is one agent.

> **Status — none of these agents are invocable yet.** Phase B (the wizard +
> registry) and Phase C (the nemoclaw runtime invocation path) aren't built.
> The files in this directory are the *target shape* — schema reference and
> design artifacts that the wizard will read/write when we get to it.

## Why agent configs are version controlled

Prompts and guardrails change agent behavior in production. We want those
changes to go through the same review process as code: PR review, history
via `git blame`, rollback to a known-good ref. Anything reachable from a
running agent (system prompt, guardrails, tool configs, memory store binding)
should be either:

- **inline** in the agent's `config.yaml` (good for ideation and tests), or
- **referenced** via a `source:` block resolving to a file in this repo
  (today) or a remote Git URL (future).

The resolver doesn't exist yet; the schema is forward-compatible so we don't
have to migrate later.

## Directory layout (proposed)

```
agents/
├── README.md                          # this file
├── network-engineer/                  # one directory per agent
│   ├── config.yaml                    # agent metadata + structured config
│   ├── system_prompt.md               # natural-language instructions
│   └── guardrails.yaml                # input/output/action/topical rails
└── <other-agent>/
    └── ...
```

One directory per agent keeps everything for that agent in one place — easy
to copy to a separate repo if/when we decide to factor agents out, easy to
fork to make a variant, easy to delete cleanly.

## Schema: `config.yaml`

```yaml
version: 1
agent:
  name: network-engineer
  description: "Short one-liner shown in the UI."
  model: claude-opus-4-7         # pin the exact model — never just "claude-opus"

  # Content fields (system_prompt, guardrails, tool configs) accept three forms:
  #
  #   1. INLINE — just write the string/structure
  #   2. FILE   — { source: file, path: relative/to/agent/dir.md }
  #   3. GIT    — { source: git, url: github.com/org/repo, path: file.md, ref: <sha|tag|branch> }
  #
  # The resolver isn't implemented yet; (1) and (2) will work first, (3) lands later.

  system_prompt:
    source: file
    path: system_prompt.md

  execution:
    mode: freeform               # or "structured" (deterministic step list)
    max_steps: 20
    max_tokens: 200000
    daily_budget_usd: 25

  inputs:
    - name: incident_id
      type: string
      required: true
    - name: target_hosts
      type: list[hostname]
      required: false

  output:
    schema:
      kind: string
      summary: string
      recommended_actions: list[string]

  tools:
    - ref: ssh-prod-readonly                  # name in the tool registry
    - ref: ansible-inventory-prod
    - ref: wireshark-parser
      config:                                 # optional per-instance config
        pcap_dir: /shared/captures

  memory:
    - ref: network-topology-db
      access: read                            # read | read_write

  access_scopes:
    - readonly-production

  guardrails:
    source: file
    path: guardrails.yaml
```

### Future: Git-backed references

Same schema, different `source`:

```yaml
system_prompt:
  source: git
  url: github.com/myorg/agent-prompts
  path: network-engineer.md
  ref: a1b2c3d              # commit SHA preferred (reproducible)
                            # tag or branch allowed; UI warns "this will follow new commits"
```

**Resolution model**: snapshot-at-save, not resolve-at-invoke. When an agent
config saves with a Git ref, the resolver fetches the content once, persists
the resolved commit SHA, and stores the snapshot in the DB. Background drift
detection polls for upstream changes and surfaces "this ref has moved" so the
operator can opt into the update. This avoids adding a network dependency to
every invocation and keeps agent runs reproducible.

**Auth (future)**: GitHub App with read-only scope on the configured repos;
token stored AES-encrypted with the existing `STARFORGE_KEY`. Deploy tokens
and self-hosted Git (Gitea, GitLab CE) are on the list but unscheduled.

## Schema: `system_prompt.md`

Plain Markdown. The agent receives the rendered text as its system prompt.
Markdown is just for human reading — the runtime sends it verbatim.

## Schema: `guardrails.yaml`

```yaml
input_rails:                        # applied to the user/caller's prompt
  - pii_redact
  - injection_check
output_rails:                       # applied to the agent's response
  - no_secrets_leaked
action_rails:                       # applied to tool/action invocations
  - on: "ssh.write|ansible.run|*.destructive"
    require: human_approval
topical:                            # keep the conversation on-topic
  allowed:
    - networking
    - incident_response
  blocked:
    - unrelated
```

The named rail policies (`pii_redact`, `injection_check`, etc.) resolve from
the nemoclaw runtime's rail registry — same pattern as the tool registry.
A rail name is a label; the actual implementation lives on the runtime side.
