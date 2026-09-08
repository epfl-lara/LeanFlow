# Running the benchmark on Linux

These are the exact files that ran the first campaign on `larapc2`
(Ubuntu 24.04, ext4, 28 cores), kept verbatim rather than tidied — they are
what provably worked. Both scripts hardcode two paths you will need to change:
`/localhome/milikic/LEAP-BENCH` (campaign directory) and
`/localhome/milikic/LeanFlow` (repo).

## Why a container at all

Ubuntu 24.04 sets `kernel.apparmor_restrict_unprivileged_userns=1`, so an
unconfined process that creates a user namespace transitions into
`/etc/apparmor.d/unprivileged_userns`, which carries `audit deny capability`.
Bubblewrap then fails with `setting up uid map: Permission denied` — and with
`--unshare-net`, earlier still, at `loopback: Failed RTM_NEWADDR`. Both are the
same denial surfacing wherever bwrap first needs a capability. Installing
bubblewrap does not help: Ubuntu's package ships no AppArmor profile, and even
`/usr/bin/unshare --user --map-root-user` fails for every user on such a host.

LeanFlow requires bubblewrap for protected Lean checks and has no unprotected
fallback, by design — `--unshare-net` is what enforces the offline clean room.

Two ways out:

- **`apparmor-bwrap.profile`** — the proper host fix, needs root. Install
  bubblewrap, copy this to `/etc/apparmor.d/bwrap`, `apparmor_parser -r` it. It
  grants `userns` to that one root-owned binary rather than disabling the
  protection host-wide via sysctl. Byte-identical in shape to the profiles
  Ubuntu already ships for `rootlesskit`, `1password` and `firefox`.
- **`Dockerfile` + `run-campaign-docker`** — no root required beyond docker
  group membership. This is what the first campaign used.

## The container

`bwrap` is installed **setuid root**, bubblewrap's supported mode for hosts
without usable unprivileged user namespaces. That lets the campaign run as the
invoking user, so bind-mounted files keep normal ownership, while bwrap can
still build its namespaces. The capability list in `run-campaign-docker` was
narrowed by bisection to the minimum that works:

    SYS_ADMIN SETPCAP NET_ADMIN SYS_CHROOT SETUID SETGID SYS_PTRACE

Dropping `SYS_PTRACE` reintroduces `bwrap: capset failed`. `--privileged` also
works and is what a naive setup reaches for; this list is materially weaker.

**ripgrep is not optional.** `session_search.py` execs a bare `rg`, and it backs
both `search_project` and the offline branch of `lean_search`. Without it the
prover has no local library search, and the `FileNotFoundError` is swallowed
into an ordinary tool error rather than raised — the first campaign ran 12 of 18
cells that way before anyone noticed. Verify after any image change:

    ./run-campaign-docker sh -c 'command -v rg'

Paths are bind-mounted at **identical locations** inside the container: the
campaign records absolute paths and verifies file digests, so they must resolve
the same on both sides.

## Operational scripts

- **`reclaim-finished-cells`** — each cell gets a private `.lake/packages` so one
  arm cannot perturb another's libraries. That is nearly free on APFS
  (copy-on-write) and a real ~7.8 GB per cell on ext4, ~140 GB across 18. This
  reclaims that space from cells that are finished for good, using an allow-list
  (`completed`, `budget_exhausted`) so an unrecognised status is protected rather
  than deleted, plus a settle delay and a `/proc`-based in-use check. Run it with
  `--apply --loop 300` alongside a campaign; peak stays ~16–24 GB.
  Do not use `fuser -m` for the in-use check: `-m` treats its argument as a mount
  point and matches every process on the filesystem, so nothing is ever reclaimed.
- **`reset-blocked-cells`** — `environment_error` is deliberately not
  auto-recovered (only `provider_error` is), so a host-level blocker needs an
  explicit reset once the host is fixed. Refuses any cell that actually spent
  provider budget, archives the attempt under `executions[]`, and removes the
  partial artifacts that would otherwise make `prepare()` fail on `exist_ok=False`.

## Lanes

`LEANFLOW_CAMPAIGN_LANES` sets concurrency (default 2). `run-campaign-docker`
forwards it — `docker run` does not inherit host environment, so setting it in
the invoking shell alone silently has no effect.
