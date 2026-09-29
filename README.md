# local-wiki

An offline reference library, curated to be useful through a long outage (6+ months): English Wikipedia plus practical repair, medical, food, water and self-reliance content. It runs on the k3s node `nas`, in the `wiki` namespace.

| What | Where |
|---|---|
| Web UI (LAN) | http://10.0.0.10:30088 |
| Web UI (public) | https://wiki.ax-h.com |
| MCP server for agents | https://wiki.ax-h.com/mcp (public) or http://10.0.0.10:30090/mcp (LAN); bearer token either way |

Everything is served from [Kiwix](https://kiwix.org) ZIM files: pre-rendered, compressed archives of websites with a built-in search index.

## Why Kiwix and not the raw Wikipedia dump

This started with the `pages-articles-multistream.xml.bz2` dump. That dump holds the *source* wikitext of each page, not rendered pages. Infoboxes, citations, unit conversions and similar all come from templates and Lua modules. The dump does include those (about 900k templates and 19k modules), but rendering them means reimplementing a large part of MediaWiki. Kiwix ZIMs are already rendered, include images, and ship with full-text search, so they give a result much closer to the real site for almost no work.

## Layout

```
k8s/kiwix.yaml          namespace, PVC, kiwix-serve Deployment, NodePort + Ingress
k8s/zim-library.yaml    the content list (zims.txt) and the zim-sync Job that downloads it
k8s/wiki-mcp.yaml       the MCP server for agents: Deployment, NodePort + Ingress
mcp/                    wiki-mcp's source (Python); see mcp/README.md
kiwix-catalog-en.tsv    snapshot of the English Kiwix catalogue (2026-09-29), for choosing content
```

Deploy or update everything with:

```sh
kubectl apply -f k8s/kiwix.yaml -f k8s/zim-library.yaml -f k8s/wiki-mcp.yaml
```

## How it works

- **Storage**: one local-path PVC, `zim`, mounted at `/data`. It holds the `.zim` files and `library.xml`. On the node it lives under `/var/lib/rancher/k3s/storage/`.
- **zim-sync Job**: walks the URLs in `zims.txt`. For each one it:
  1. skips the file if it's already present;
  2. otherwise downloads it to `<name>.zim.part` with `wget -c`, so a restarted pod resumes where it stopped;
  3. checks it against Kiwix's published `.sha256`;
  4. renames it to `.zim` and registers it in `library.xml` with `kiwix-manage`.

  If another process is still writing a `.part` file, the Job waits for it instead of racing it.
- **kiwix-serve** runs with `--monitorLibrary --library /data/library.xml`. It re-reads the library within about a second of any change, so new content goes live without a restart.
- **wiki-mcp** reads everything through kiwix-serve's Service, so it sees new content as soon as kiwix does.

The kiwix-serve image has its own `DOWNLOAD` env var. Don't use it: it re-downloads from scratch, without resuming, on every pod start.

## Content

The current list, about 31 GB plus Wikipedia (about 158 GB in total):

- **Core**: Wikipedia (maxi, with images), Wikibooks, Wiktionary, iFixit, NHS Medicines A–Z, OpenStreetMap UK
- **Stack Exchange Q&A**: DIY, Gardening, Motor Vehicle Mechanics, Amateur Radio, Cooking, Outdoors, Electronics, Bicycles, Woodworking, Homebrewing, Sustainable Living, Pets, Engineering
- **Preparedness**: picked from Kiwix's "preppers" bundle, without the Canadian/US-specific channels:
  - water treatment
  - food preparation, USDA home canning
  - post-disaster library, CD3WD
  - military medicine, field medical library
  - knots, Restarters, WikiCiv

Left out on purpose: Canadian Prepper, S2 Underground, Urban/True Prepper, Ready.gov, the privacy guides, and Survivor Library.

### Adding, removing or updating content

1. Find the file in `kiwix-catalog-en.tsv` or at https://library.kiwix.org. The `url` column gives the download link; use the `download.kiwix.org` host.
2. Edit `zims.txt` in `k8s/zim-library.yaml`.
3. Re-run the sync. It only fetches what's missing:

   ```sh
   kubectl -n wiki delete job zim-sync --ignore-not-found
   kubectl apply -f k8s/zim-library.yaml
   kubectl -n wiki logs -f job/zim-sync | grep -E '^(downloading|ready)'
   ```

Removing a URL from the list doesn't delete the file or unregister it. To do that, run `kiwix-manage /data/library.xml remove <book-id>` and delete the file from the PVC.

Kiwix publishes new builds every few months. The date is in the filename, so updating means swapping the URL and removing the old file.

## Using it

### Web

Each source has its own search bar: it suggests titles as you type, and Enter runs a full-text search. The box on the home page only filters the *list of sources*; it doesn't search their contents. To search everything at once, use:

```
https://wiki.ax-h.com/search?pattern=your+words
```

Some sources have no full-text index, so only title search works for them: iFixit, the UK map, the zimgit libraries, and a few other small ones.

### MCP (agents)

[wiki-mcp](mcp/README.md) is our own small MCP server, built for local models. It serves streamable HTTP at https://wiki.ax-h.com/mcp, and on the LAN at http://10.0.0.10:30090/mcp. It has two tools:

- `search_library(query)` covers the whole library in one call. That includes the PDF books in the zimgit libraries and the title-only sources such as iFixit, which kiwix's own search can't see.
- `read_library(url, find=…, offset=…)` returns a hit as markdown. `find` jumps to the passages about a topic, which is the way to use long PDF books.

Every request to `/mcp` needs the bearer token; without it the server answers 401. Only the exact `/mcp` path is routed publicly, so the unauthenticated `/healthz` stays internal. `MCP_ALLOWED_HOSTS` pins the accepted Host headers, for DNS-rebinding protection.

The token lives in the Secret `wiki-mcp-token`. To create it (once):

```sh
kubectl -n wiki create secret generic wiki-mcp-token --from-literal=token="$(openssl rand -hex 32)"
```

To rotate the token, delete the Secret, recreate it, and run `kubectl -n wiki rollout restart deploy/wiki-mcp`. Then update your clients.

**Claude Code:**

```sh
claude mcp add --transport http --scope user wiki https://wiki.ax-h.com/mcp \
  --header "Authorization: Bearer $(kubectl -n wiki get secret wiki-mcp-token -o jsonpath='{.data.token}' | base64 -d)"
```

**LM Studio** (`mcp.json`; use `https://wiki.ax-h.com/mcp` away from home):

```json
{ "mcpServers": { "wiki": { "url": "http://10.0.0.10:30090/mcp",
    "headers": { "Authorization": "Bearer <token>" } } } }
```

**Deploying a new build**: pushing to `main` under `mcp/` runs the `mcp` workflow. It tests, builds and smoke-tests the image, then pushes `ghcr.io/axle-h/local-wiki-mcp:<sha>`. Put that tag in `k8s/wiki-mcp.yaml` and `kubectl apply` it. The Deployment pins a commit tag rather than `:latest` on purpose: `:latest` makes Kubernetes pull from ghcr on every pod start, which fails without internet, while a pinned tag starts from the copy already on the node.

## Networking

- **LAN**: NodePorts 30088 (web) and 30090 (MCP) on 10.0.0.10.
- **Public**: `wiki.ax-h.com` serves the web UI, and `wiki.ax-h.com/mcp` serves MCP. The MCP route is a second Ingress with Traefik `router.priority: 100`: Traefik ranks routes by rule length, so without it kiwix's `PathPrefix(/)` rule would win. It reuses the kiwix Ingress's certificate and has no cert-manager annotation of its own. Both follow the same pattern as the other apps on the cluster:
  - Traefik Ingress
  - cert-manager with `letsencrypt-production`; the certificate renews automatically
  - the namespace's `redirect-http-https` middleware

  DNS is kept current by the hourly `ddns` CronJob, whose `DOMAINS` list includes `wiki.ax-h.com`.

Kiwix search is the most CPU-hungry part, and the node has 4 cores. If crawlers become a problem, put the ingress behind SSO or add a Traefik rate-limit middleware.

## Resilience notes

- If the grid is down, so is the NAS. Keep a second copy of the `.zim` files on a laptop or USB drive; the Kiwix desktop and Android apps read them directly, with no server needed.
- Install on your phones: Kiwix (Android), Trail Sense, Survival Manual.

## Housekeeping

- The PVC requests 200Gi. local-path doesn't enforce that size, and the node has about 880 GB free.
- A finished `zim-sync` Job deletes itself after a day (`ttlSecondsAfterFinished`), so re-running the sync is just `kubectl apply`.
