# Droplet redeploy runbook — `polymarket_hybrid_bot` v0.2.8 → main (`c6dd38f`)

**Audience:** operator with SSH to `root@157.230.63.174`.
**Source repo state assumed in this runbook:** `main` at commit `c6dd38f`, confirmed via local read in your workstation repo.
**Droplet state assumed (verify on box first — see §0):** `swarm/team-b-phase-d` at `77b3ccd` (tag `v0.2.8`), upstream gone; dirty `docker-compose.yml` with a `127.0.0.1:8080:8080` loopback ports block (comment `D.2.3`); three `.bak` files untracked; running container `polymarket-hybrid-bot` on image `polymarket_hybrid_bot:0.2.4`, healthy.

**Irreversibility legend:** ⚠️ = hard to undo without rollback; 🛑 = decision point requiring human input.

---

## §0 — Land on the droplet and verify assumed state

> Every step below assumes the droplet state described in this section actually matches. Stop and reassess if any check disagrees.

```bash
ssh root@157.230.63.174
cd /opt/polymarket_hybrid_bot/polymarket_hybrid_bot   # inner repo
git status -sb
git rev-parse HEAD                          # expect 77b3ccd
git describe --tags --exact-match 2>/dev/null  # expect v0.2.8
git diff docker-compose.yml | head -40      # expect the loopback ports block
ls -1 docker-compose.yml.bak.*              # expect 3 backup files
docker ps --filter name=polymarket-hybrid-bot --format \
  'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

Expected: container `polymarket-hybrid-bot` listed as `Up X (healthy)`, image `polymarket_hybrid_bot:0.2.4`.

**Locate the OUTER compose dir** (where `rebuild.sh` is meant to be run — `rebuild.sh` `cd`s to repo root, which is the outer dir per coordinator recon):

```bash
ls /opt/polymarket_hybrid_bot/         # confirm outer layout
ls /opt/polymarket_hybrid_bot/polymarket_hybrid_bot/deploy/rebuild.sh
```

If anything above disagrees with the assumed state, stop and re-verify before continuing.

---

## §1 — Pre-flight: capture rollback reference ⚠️ (irreversible reference loss if skipped)

Before touching anything, capture the running container's image **ID** (not just the tag — the tag `0.2.4` will be reused by the next build):

```bash
# Capture image ID of the running container
ROLLBACK_IMAGE=$(docker inspect --format '{{.Image}}' polymarket-hybrid-bot)
echo "$ROLLBACK_IMAGE" | tee /root/rollback_image_id.txt
# Expected: a sha256:... string, e.g. sha256:abc123...

# Confirm it's resolvable (independent of the :0.2.4 tag)
docker image inspect "$ROLLBACK_IMAGE" --format '{{.Id}} {{.Created}}' \
  | tee -a /root/rollback_image_id.txt

# Also capture container's current env+labels for forensic reference
docker inspect polymarket-hybrid-bot > /root/rollback_inspect.json
```

> Why image ID not tag: the next `docker compose build` will overwrite tag `polymarket_hybrid_bot:0.2.4` (compose at `c6dd38f` still pins that tag). Without the ID captured beforehand, you cannot pin-rollback to the currently-healthy build.

Capture current healthcheck + log tail for comparison:

```bash
docker logs --tail 30 polymarket-hybrid-bot
```

---

## §2 — 🛑 RAM decision point: resize vs. rely on existing swap

The build of `cytoolz`/`web3` (native extensions) is the OOM hazard on a 1 GB droplet. The droplet reportedly has swap; verify the actual numbers before deciding.

```bash
# Real numbers
free -h
swapon --show
cat /proc/sys/vm/swappiness        # 10 is the value from deploy/DEPLOY_DO.md Step 0
df -h /                            # need ~2GB free for the build context
```

**Decide one of two paths based on what you see:**

### Option 2A — Resize droplet to 2 GB (durable; recommended if you have budget)

⚠️ Irreversible without resize-down; downtime ~1–2 min while the droplet reboots.
Per the **root** `DEPLOY_DO.md` (lines 6, 54), 2 GB is the documented minimum and the standard OOM remedy is "upgrade droplet to 2 GB; rerun `./deploy/rebuild.sh --nuke`".

1. In the DO web console, power off the droplet → Resize → pick `s-1vcpu-2gb` (same NYC1 region) → resize → power on.
2. SSH back in and re-verify §0.
3. Proceed to §3.

### Option 2B — Rely on existing swap (no cost, no downtime, but risky)

If `free -h` shows ≥1 GB swap free AND `swappiness=10`, the build *should* succeed but builds will be slow under swap pressure and you risk thrash. The droplet's existing 1 GB swap (per `deploy/DEPLOY_DO.md` Step 0) is the documented mitigation for 1 GB hosts. **If swap is <1 GB or `swappiness > 30`, do not use this path — go to 2A.**

Optionally add a temporary build-time additional swap (delete after deploy):

```bash
# Only if existing swap is insufficient — adds 1 GB more, removable later
fallocate -l 1G /swapfile.tmp && chmod 600 /swapfile.tmp \
  && mkswap /swapfile.tmp && swapon /swapfile.tmp
free -h     # confirm swap grew
# Remember to: swapoff /swapfile.tmp && rm /swapfile.tmp     after §6 success
```

Proceed to §3.

---

## §3 — Reconcile droplet git: `swarm/team-b-phase-d` → `main@c6dd38f`, preserving the loopback ports edit

⚠️ This section modifies the working tree. Make sure §1 rollback reference is saved.

The dirty file is `docker-compose.yml` with a `127.0.0.1:8080:8080` ports block that does **not** exist in `main` at `c6dd38f` (verified locally: compose at `c6dd38f` has no `ports:` block). The local edit must be preserved (Option A from the lock).

```bash
cd /opt/polymarket_hybrid_bot/polymarket_hybrid_bot   # the inner repo

# 1. Stash ONLY the tracked dirty file. Do NOT use -u — that would stash the
#    .bak files too, which we'll leave alone for now.
git stash push docker-compose.yml -m "loopback-ports-edit-pre-main-reconcile"
git stash list                              # confirm one new stash entry

# 2. Verify working tree is now clean (untracked .bak files are fine)
git status -sb

# 3. Fetch and prune. This removes the stale remote ref for the gone
#    upstream branch (swarm/team-b-phase-d), but leaves the local branch.
git fetch origin --prune
git log --oneline -1 origin/main           # expect c6dd38f

# 4. Force local main to match origin/main (creates main if it doesn't exist
#    locally, resets it if it does). The orphaned swarm/team-b-phase-d local
#    branch is left in place; cleanup is §8.
git checkout -B main origin/main
git rev-parse HEAD                          # expect c6dd38f

# 5. Reapply the loopback ports edit.
git stash pop                               # may report a 3-way merge
git status -sb                              # expect: modified docker-compose.yml
git diff docker-compose.yml                 # confirm: ONLY the ports block added

# 6. If `git stash pop` reports a conflict (unlikely — compose lines around
#    the insertion point are unchanged between v0.2.8 and c6dd38f), STOP and
#    resolve by hand: open docker-compose.yml, ensure the ports: block is
#    inserted in the bot service, then `git checkout --theirs` is NOT correct
#    here; edit by hand and `git add docker-compose.yml`, then `git stash drop`.
```

Sanity-check the final compose:

```bash
grep -nE '^\s*(image:|ports:|127\.0\.0\.1:8080)' docker-compose.yml
# Expect:
#   image: polymarket_hybrid_bot:0.2.4
#   ports:
#     - "127.0.0.1:8080:8080"    (or similar)
```

> Note: this leaves the working tree with one tracked modification (the ports block) as a documented local diff. That matches the droplet's prior state but is now anchored against `main@c6dd38f` instead of `v0.2.8`.

---

## §4 — Rebuild ⚠️ (irreversible without §7 rollback; bot is DOWN during build)

`rebuild.sh` runs `docker compose down --remove-orphans` BEFORE `docker compose build` (lines 71 and 78). A failed build leaves the bot stopped until you rollback per §7.

```bash
cd /opt/polymarket_hybrid_bot/polymarket_hybrid_bot
./deploy/rebuild.sh --no-tail
```

Expected (success) output, in order:
1. `docker compose down --remove-orphans` — old container stops and is removed.
2. `docker compose build --pull` — pulls base image, runs Dockerfile. **This is the OOM window.** On a 2 GB droplet this is typically 3–6 minutes; on 1 GB+swap it may take 10–20 minutes and is the most likely failure point.
3. `docker compose up -d` — new container started.
4. Polls `docker compose logs --since=2m bot` for up to **60 seconds**, grepping for `strategy_loop_started`.
5. On success, prints: `OK: strategy_loop_started observed in bot logs.` and exits 0 (because `--no-tail`).
6. On failure: prints `ERROR: strategy_loop_started not found within 60s` + last 50 log lines to stderr, exits **2**. ⚠️ Go to §7.

If the build itself OOMs, you'll see a non-zero exit from `docker compose build` with messages like `Killed`, `MemoryError`, or `c1: out of memory` (from `cc1`/`gcc` building `cytoolz`). Go to §7.

Pre-flight check `rebuild.sh` enforces:
- `.env` must exist and be mode 600 (lines 7–34). If you previously ran `chown -R 1000:1000` (§6), `.env` is now owned by UID 1000; the mode 600 check should still pass since `stat -c %a` is mode-only.

---

## §5 — Smoke test new container (positive + negative)

### 5.1 Container-level health
```bash
docker ps --filter name=polymarket-hybrid-bot --format \
  'table {{.Names}}\t{{.Status}}\t{{.Image}}'
# Expect Status: Up X (healthy) within 120s (start_period in healthcheck).

docker inspect polymarket-hybrid-bot --format \
  '{{.State.Health.Status}} {{.State.Health.FailingStreak}}'
# Expect: healthy 0

docker logs --tail 50 polymarket-hybrid-bot | grep -E \
  'strategy_loop_started|strategy_loop_heartbeat|polymarket_feed_heartbeat|spot_feed_heartbeat'
# All four event names should appear within ~3–5 minutes of startup.
```

### 5.2 Snapshot endpoint via SSH local-forward (positive smoke)

This is why §3 preserves the loopback ports block.

From your **workstation** (not the droplet), in a separate terminal:

```bash
ssh -N -L 8080:127.0.0.1:8080 root@157.230.63.174
# Leave running; Ctrl-C when done.
```

Then on the workstation:

```bash
# Find the dashboard auth token name. The bot's .env on the droplet defines
# it — I do not have the exact env var name verified for c6dd38f. On the
# droplet, grep first:
#     ssh root@157.230.63.174 'grep -E "TOKEN|DASHBOARD" /opt/polymarket_hybrid_bot/polymarket_hybrid_bot/.env'
# Use whichever token-bearing var matches the api/ snapshot handler.

curl -sS -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8080/dashboard/snapshot \
  | python3 -m json.tool | head -40
# Expect a JSON object with mode, equity, positions[], risk{}, equity_history[],
# logs[], alerts[] keys.
```

> Note: the exact snapshot path and auth-header shape were not verified against `c6dd38f`'s `api/` package while drafting this runbook. Treat the curl line as a template; if the response is 404 or 401, inspect `api/` routes on the droplet and adjust.

### 5.3 Negative smoke (auth required)

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/dashboard/snapshot
# Expect: 401 (or 403). NOT 200.
```

### 5.4 Heartbeat-cadence verification (let it run ~3 min, then check)
```bash
docker logs --since=3m polymarket-hybrid-bot | grep -c strategy_loop_heartbeat
# Expect: ≥1 (cadence per main.py HEARTBEAT_EVERY)
```

If any check above fails, go to §7.

---

## §6 — Conditional: chown fix for `market_cache_write_failed`

Only run this if **after §5** you see this in the logs:

```
{"error": "[Errno 13] Permission denied: '.market_cache.json'",
 "event": "market_cache_write_failed", "level": "warning"}
```

Per **root** `DEPLOY_DO.md` lines 60–78:

```bash
chown -R 1000:1000 /opt/polymarket_hybrid_bot
cd /opt/polymarket_hybrid_bot/polymarket_hybrid_bot
docker compose up -d            # NOT `restart` — recreate so bind-mount UID is re-read
```

Then re-verify §5.1 and grep for `market_cache_write_failed` — should no longer appear.

> Caveat: `chown -R 1000:1000 /opt/polymarket_hybrid_bot` changes ownership of `.env` to UID 1000 too. That's fine for `rebuild.sh`'s mode-only check, and matches the in-container UID.

---

## §7 — Rollback to the captured `0.2.4` image ID

Trigger conditions: build OOM, `rebuild.sh` exit 2, snapshot smoke fails, or new container unhealthy after 5 minutes.

```bash
cd /opt/polymarket_hybrid_bot/polymarket_hybrid_bot
ROLLBACK_IMAGE=$(cat /root/rollback_image_id.txt | head -1)
echo "Rolling back to: $ROLLBACK_IMAGE"

# Stop the failing container if it's running
docker compose down --remove-orphans

# Re-tag the captured image so compose's image: line resolves to it
docker tag "$ROLLBACK_IMAGE" polymarket_hybrid_bot:0.2.4

# Bring the old image back up (no build)
docker compose up -d --no-build

# Verify healthy
sleep 90
docker ps --filter name=polymarket-hybrid-bot --format \
  'table {{.Names}}\t{{.Status}}\t{{.Image}}'
docker inspect polymarket-hybrid-bot --format \
  '{{.State.Health.Status}}'                         # expect: healthy

docker logs --tail 30 polymarket-hybrid-bot | grep strategy_loop_started
```

If rollback succeeds, the bot is back on the previously-healthy `0.2.4` image. The git state is `main@c6dd38f` with the loopback ports edit applied — keep it; only the container image is rolled back. Investigate the build failure separately.

If rollback also fails: stop and escalate. Do not iterate blindly.

---

## §8 — Optional cleanup (DO NOT bundle with deploy — separate, post-success)

Run only after §5 passes AND you've decided not to roll back. Each item is independent.

### 8a — Stale images
```bash
docker images polymarket_hybrid_bot      # confirm 0.2.1 and old 0.2.4 layers
docker images deploy-polymarket-hybrid-bot
# Inspect before deleting
docker image rm polymarket_hybrid_bot:0.2.1 deploy-polymarket-hybrid-bot:latest
# (If `docker image rm` refuses because containers reference the image, leave it.)
```

### 8b — Orphaned local git branch
```bash
cd /opt/polymarket_hybrid_bot/polymarket_hybrid_bot
git branch -D swarm/team-b-phase-d        # only after you've confirmed main is fine
```

### 8c — `.bak` files
```bash
ls -1 docker-compose.yml.bak.*
# These are not tracked. Confirm none of them differ in a way you want to
# preserve, then:
rm docker-compose.yml.bak.pre-d23 \
   docker-compose.yml.bak.pre-restore \
   docker-compose.yml.bak.pre-v0.2.8
```

### 8d — Temporary swap file from §2B (if you added one)
```bash
swapoff /swapfile.tmp && rm /swapfile.tmp
free -h
```

### 8e — Rollback breadcrumbs (only if you're past the maintenance window)
```bash
# Keep these for at least 24h after a successful deploy.
ls /root/rollback_image_id.txt /root/rollback_inspect.json
# Delete only after you're sure no rollback is needed.
```

---

## Noted separately (not part of this deploy)

- **Documentation drift:** there are two `DEPLOY_DO.md` files. CLAUDE.md's doc-map names `deploy/DEPLOY_DO.md` as canonical, but the operationally critical content (2 GB minimum, OOM table, chown fix, Updates section) lives in **`/DEPLOY_DO.md`** (repo root). This runbook references the root file for those bits. Reconciling the two is a documentation chore, not part of this deploy. (Known-issue territory; flag in `docs/NEXT_STEPS.md`.)
- **Image tag reuse:** compose still pins `polymarket_hybrid_bot:0.2.4` at `c6dd38f`, even though `c6dd38f` is 5 commits ahead of v0.2.4 / v0.2.8. Future rebuilds will keep overwriting the tag — rollback by ID is the only safe path. Worth bumping the tag in a follow-up PR.
- **`mem_limit: 1g` on a 1 GB host:** under runtime memory pressure the container can starve the host. Not addressed here; surfaces only post-deploy. Track if observed.
- **Metrics port:** compose at `c6dd38f` does not map `9091`. The `/metrics` endpoint is unreachable except from inside the docker network. Out of scope for this deploy but worth flagging.
