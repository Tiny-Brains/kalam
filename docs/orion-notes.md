# Orion — what building Kalam found

The Orion facts building Kalam's package turned up, which no design document had written down.

> **Moved from `devops/docs/` on 17 September 2026**, when devops stopped running anything (N25).
> The page was split by the repository each section is about, and every section keeps the number the
> whole page gave it, so a citation of *§0c* still resolves — in the repository that section now
> lives in. Every other section is [soma's](https://github.com/Tiny-Brains/soma/blob/main/docs/orion-notes.md).
> It was written before N25: where it describes in-cluster Kalam replicas, a loader, an Orion image
> devops built or `docker-compose.fleet.yml`, the platform now runs a Soma node image, runners from
> kalam's compose file, and web's `docker-compose.yml`.

## 0c. A skipped connector does not fail the package; it fails every workflow that names one

Found on 16 September 2026, building the standalone runner, and it invalidates a premise the whole
off-site design was resting on.

The premise, and it is true: an `env://` reference a connector cannot resolve makes Orion log
`Failed to resolve secret reference in connector config, skipping` and **carry on**. The package
installs. That is what lets a machine hold no database credential — omit `KALAM_DB_URL` and the
`kalam-db` connector simply is not there.

What the premise does not say is what happens next. `package apply` activates in dependency order
and **stops at the first workflow it cannot activate**:

```
error: activating workflows 'tb-match-run': HTTP 400 VALIDATION_ERROR:
  connector(s) 'kalam-db', 'kalam-blobs' not found — create them first, or fix the reference
Error: activation stopped at workflows 'tb-match-run'. Everything before it is active but the
  engine has NOT been reloaded; everything after is staged as drafts.
```

Every channel after that point stays a **draft**, and a draft cron channel holds no schedule. So the
node installed its package, loaded its wasm plugin, answered `/readyz` 200 — and was completely
inert. All five channels draft, nothing claimed, nothing logged.

**Empty is not absent.** `KALAM_DB_URL: ""` *resolves*; the connector loads, is useless, and the
workflows activate. The task that would use it is gated on a var and never runs. That is why
`docker-compose.fleet.yml` sets empty strings rather than omitting the keys, and the reason is not
the one its comment gave: it is not that Orion skips the connector, it is that the workflow must
still find it.

**And a connector needs EVERY `env://` it names.** Leaving `R2_BUCKET` out while setting
`R2_ACCESS_KEY`, `R2_SECRET_KEY` and `R2_ENDPOINT` was enough to skip `kalam-blobs` and reproduce the
whole failure.

**The rule:** `/readyz` and the plugin list are not evidence a node will do any work. The assertion
is the count of **active** channels carrying the package's tag — which is what `compose/loader/run.sh`
has always done for the cluster, and what a self-loading runner has to do for itself.

---

## 2. Kalam and the wave workflow

Eleven more, found while building the wave workflow. The first is the one
that cost the most and is the one to remember.

| Finding | What it means |
|---|---|
| **`metadata.vars` is ROOT scope, exactly as `data` is.** A `[vars]` value read inside a `map` or `filter` body is `null` | Combined with `{">=": [0, null]}` being **true**, reading `strike_ceiling` inside the strike map forfeits every seat on turn 0. The next turn sends the loader nothing and the wave dies at `step` with "0 actions for 10 live seats" — an error naming neither the variable nor the cause. The spike's finding 2.6 was about `data`; it is about vars too, and the consequence is worse because the failure is *plausible*. Every such accumulator is now a `reduce` with the value in its seat, and the workflow's first task halts loudly if any var is missing |
| **`http_call` has no `url` field**: `path` is always appended to the connector's base | So a presigned URL cannot be used directly — `base + url` is what gets fetched. The replay `PUT` reduces the presigned URL to a path with `substr(url, length(base))` |
| **`force_path_style` on a storage connector is what makes that subtraction exact** | Without it the presigned URL is virtual-hosted (`bucket.host/key`), and the prefix to strip is a string neither side is configured with. With it the URL is `endpoint/bucket/key` and the prefix is exactly the endpoint |
| **`storage_presign` returns a plain string**, not an object; and `storage_presign` and `storage_head` are Orion's *only* storage task functions | Orion carries no bytes by design, so a replay write is presign + an ordinary `http_call`. That is why Kalam ships a second blob connector |
| **`http_call` parses the reply as JSON unless told otherwise** | An S3 `PUT` answers with an empty body, so the task fails on "EOF while parsing a value" *after the write succeeded*. `response_format: "text"` |
| **An http connector's `url` may not be an `env://` reference** — `VALIDATION_ERROR: must use http or https scheme, got 'env'`. A storage connector's `endpoint` may | `kalam/connectors/model-loader.json` as shipped could never load. The load script substitutes both URLs from the environment now, as it already did for the SSRF flag |
| **A plugin is *archived*, not deleted, and cannot be archived while an active workflow calls its functions** | The load script's delete-by-tag sweep was a silent no-op for plugins, so the *second* load of a package 409s. It works on a fresh server, which is why this hides until the first redeploy |
| **`{"now": []}` exists** and returns an ISO-8601 instant | Which is where `played_ms` comes from: the workflow captures the wave's start and Postgres does the subtraction |
| **`{"merge": [A, B]}` concatenates two computed arrays** (while `{"merge": <one expression>}` flattens nothing) | Both behaviours are load-bearing: one appends the carried-forward forfeited refs, the other is why flattening a list of lists needs a `reduce` |
| **A REST channel needs top-level `name`, `methods` and `route_pattern`**, and `config.response.mode` is `envelope` or `shaped` | Kalam's original channel sketch had none of them. The same class of error as "every task needs a `name`" |
| **A cron occurrence's data is unreadable**: it returns nowhere, and a trace record carries no per-task detail (and drops it above `queue.max_result_size_bytes`, which a wave exceeds at 4.2 MB) | So a wave that goes wrong can only be diagnosed by *which* task failed. Kalam's config now mounts a development-only REST path so the same workflow can be driven by hand and its `data` read — without which the two bugs above would still be unfound |

The cron spellings recorded during the schema build were **confirmed against the 1.7.0 source**, not assumed:
`transport_config` takes `schedule`, `timezone`, `misfire_policy` (`skip`/`latest`/`catch_up`) and
`concurrency: {policy, key}`, all under `deny_unknown_fields`; `metadata.trigger` carries `type`,
`occurrence_id`, `scheduled_for`, `started_at`, `timezone`, `attempt` and `singleton_key`. A cron
channel is `channel_type: "async"`, `protocol: "cron"`.
