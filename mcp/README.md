# wiki-mcp

An MCP server that lets an agent search and read the offline library. It is built for small local models: there are two tools, and neither needs the model to know which source to look in.

| Tool | Does |
|---|---|
| `search_library(query, limit=10)` | Searches the whole library at once and returns titles, sources, snippets and a `url` for each hit. |
| `read_library(url, find=None, offset=0)` | Returns a page, article or PDF book as markdown, a part at a time. `find="tourniquet"` returns only the passages about that. |

It sits in front of kiwix-serve and adds nothing to the library itself.

The tool descriptions do most of the work of getting a model to use the tools at all. Many clients, LM Studio among them, never show the model the server's `instructions`, so each description says on its own what the library holds and when to reach for it. That list lives in `LIBRARY` in `src/wiki_mcp/server.py`; keep it in step with `k8s/zim-library.yaml`.

## What `search_library` covers

Results come in up to four sections.

**Encyclopedia and handbook** comes first. kiwix's ranking across all sources tends to bury the obvious article under loosely related Q&A threads: "treat a burn" ranks threads about treated wood above Wikipedia's *Burn*. So Wikipedia and Wikibooks are searched on their own as well, and two signals are combined, strongest first:

1. **Exact titles.** A Wikipedia article whose title is a multi-word part of the query (*Car battery*, which redirects to *Automotive battery*).
2. **Per-source search.** Wikipedia and Wikibooks hits whose title contains every keyword.
3. **Exact titles again.** A Wikipedia article whose title is a single keyword (*Burn*). Only for one- or two-keyword queries, and only if the article also mentions the other keyword, so "hypothermia symptoms" doesn't surface *Symptom*.
4. **Per-source search again.** Hits whose title contains some keyword.

Disambiguation pages are skipped, and per-source hits whose title shares no keyword are dropped.

**Everything else** is kiwix's own full-text search across every source that has a full-text index. Two kinds of source fall outside it, and `search_library` covers them itself:

- **Title-only sources** have no full-text index; iFixit is one. They are searched through kiwix's per-source title suggestions and listed under *Matching titles*.
- **PDF libraries** are the zimgit medical, water, food, post-disaster and knots collections. Each is a JavaScript page over a `database.js` that lists each PDF's title, description and author. kiwix indexes none of that, so wiki-mcp loads those lists and matches them itself, listed under *Books and manuals*.

Map sources are skipped: they have no text to read.

## What `read_library` returns

- **MediaWiki pages** (Wikipedia, Wikibooks, Wiktionary, …): the article body, with navigation, edit links, reference lists and navboxes removed.
- **Stack Exchange pages**: the question, then each answer with its score.
- **PDFs**: the text with `[page N]` markers. Extracting a big book takes a few seconds the first time; the 16 most recently read documents are cached.
- **Links and images are dropped.** `search_library` is how a model gets around, and links cost tokens.

## Configuration

| Env var | Default | |
|---|---|---|
| `KIWIX_URL` | `http://kiwix.wiki.svc.cluster.local` | kiwix-serve's base URL |
| `MCP_AUTH_TOKEN` | required | The bearer token. `Authorization: <token>` without `Bearer` is accepted too. |
| `MCP_INSECURE_NO_AUTH` | unset | `1` runs without a token; for local testing only |
| `MCP_ALLOWED_HOSTS` | none (check off) | Comma-separated Host headers to accept; loopback is always allowed |
| `PORT` | `8000` | |
| `READ_PAGE_CHARS` | `8000` | How much text `read_library` returns per call |

The transport is streamable HTTP at `/mcp`. It is stateless and answers in JSON, so a pod restart drops no sessions. `/healthz` needs no token and never calls kiwix.

## Development

```sh
uv sync
uv run pytest                 # unit tests, against recorded kiwix responses in tests/fixtures
uv run ruff check && uv run ruff format --check
KIWIX_URL=http://10.0.0.10:30088 MCP_AUTH_TOKEN=dev uv run wiki-mcp    # serves :8000
```

CI (`.github/workflows/mcp.yml`) runs the checks. It then builds the image, runs it the way Kubernetes does (read-only, no capabilities, non-root), and checks that `/mcp` refuses a missing token and lists the two tools. On `main` it pushes `ghcr.io/axle-h/local-wiki-mcp:<sha>` and `:latest`.
