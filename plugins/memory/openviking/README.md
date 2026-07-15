# OpenViking Memory Provider

Context database by Volcengine (ByteDance) with filesystem-style knowledge hierarchy, tiered retrieval, and automatic memory extraction.

## Requirements

- `pip install openviking`
- OpenViking server running (`openviking-server`)
- Embedding + VLM model configured in `~/.openviking/ov.conf`

## Setup

```bash
hermes memory setup    # select "openviking"
```

The setup can link to an existing `~/.openviking/ovcli.conf`, copy its current
connection values into Hermes, or create a minimal `ovcli.conf` when one does
not exist.

Or manually:
```bash
hermes config set memory.provider openviking
echo "OPENVIKING_ENDPOINT=http://localhost:1933" >> ~/.hermes/.env
```

## Config

All config via environment variables in `.env`:

| Env Var | Default | Description |
|---------|---------|-------------|
| `OPENVIKING_ENDPOINT` | `http://127.0.0.1:1933` | Server URL |
| `OPENVIKING_API_KEY` | (none) | User/admin API key for authenticated servers |
| `OPENVIKING_ACCOUNT` | `default` | Tenant account for local/trusted mode |
| `OPENVIKING_USER` | `default` | Tenant user for local/trusted mode |
| `OPENVIKING_AGENT` | `hermes` | Hermes peer ID in OpenViking, used for peer-scoped memories |
| `OPENVIKING_AUTO_SYNC` | `true` | Automatically mirror conversation turns into OpenViking sessions |
| `OPENVIKING_AUTO_PREFETCH` | `true` | Automatically search and inject OpenViking context before a turn |
| `OPENVIKING_AUTO_COMMIT` | `true` | Automatically commit sessions on end/switch; commits may invoke OpenViking summary/extraction models |
| `OPENVIKING_MIN_COMMIT_TURNS` | `1` | Minimum mirrored turns required before an automatic commit |
| `OPENVIKING_SYNC_USER_CHAR_LIMIT` | `4000` | Maximum user characters mirrored per simple turn |
| `OPENVIKING_SYNC_ASSISTANT_CHAR_LIMIT` | `4000` | Maximum assistant characters mirrored per simple turn |
| `OPENVIKING_PREFETCH_TOP_K` | `6` | Legacy fallback for automatic recall count when `OPENVIKING_RECALL_LIMIT` is unset |

The three automatic lifecycle controls only gate implicit provider activity. Explicit
`viking_search`, `viking_read`, `viking_remember`, `viking_forget`, and
`viking_add_resource` calls remain available. For a low-cost explicit-only setup,
set all three `OPENVIKING_AUTO_*` values to `false`; this also prevents automatic
session commits from generating archive summaries.

When `OPENVIKING_API_KEY` is set, Hermes lets OpenViking derive account/user
identity from the key. In local or trusted deployments without an API key,
Hermes sends `OPENVIKING_ACCOUNT` and `OPENVIKING_USER` as identity headers.

## Tools

| Tool | Description |
|------|-------------|
| `viking_search` | Semantic search with fast/deep/auto modes |
| `viking_read` | Read content at a viking:// URI (abstract/overview/full) |
| `viking_browse` | Filesystem-style navigation (list/tree/stat) |
| `viking_remember` | Store a fact directly with OpenViking `content/write` |
| `viking_forget` | Delete one exact `viking://` memory file URI |
| `viking_add_resource` | Ingest URLs/docs into the knowledge base |

## Memory Writes And Deletes

`viking_remember` writes directly to OpenViking with `POST /api/v1/content/write`
and `mode=create`. It creates peer-scoped memory files under
`viking://user/peers/${OPENVIKING_AGENT}/memories/...`; OpenViking may return a
canonical user-scoped form such as
`viking://user/default/peers/${OPENVIKING_AGENT}/memories/...` in API-key mode.
Explicit remembers do not depend on session commit extraction.

Hermes built-in `memory` tool additions are mirrored to OpenViking after the
local memory operation succeeds:

| Hermes action | OpenViking operation |
|---------------|----------------------|
| `add` | `content/write` with `mode=create` under the configured peer memory namespace |

Built-in `replace` and `remove` operations are not mirrored because Hermes
native memory entries do not yet carry stable OpenViking file URIs. Use
`viking_forget` when the user explicitly asks to delete a specific OpenViking
memory URI.

`viking_forget` is intentionally narrow. It only accepts concrete user memory
file URIs, such as
`viking://user/peers/hermes/memories/preferences/mem_abc123.md` or the canonical
`viking://user/default/peers/hermes/memories/preferences/mem_abc123.md`. Files
directly under `memories/`, such as `viking://user/default/memories/profile.md`,
are also allowed because OpenViking supports them. The tool rejects directories,
resources, skills, sessions, generated summary files, and URIs with query
strings or fragments. Use OpenViking's MCP, CLI, or admin APIs for broader
resource and directory cleanup.
