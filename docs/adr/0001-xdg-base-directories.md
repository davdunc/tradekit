# ADR 0001 — XDG base directories for on-disk layout

**Status:** Accepted
**Date:** 2026-07-25

## Context

tradekit built every on-disk path inline from `Path.home()`, scattered across
eight modules. Three problems followed from that.

**The paths encoded one developer's machine.** `~/OneDrive/Documents/Trade_Review`
and `~/Projects/falcon/.env` were hardcoded defaults in source. Neither resolves
on a second machine, and neither resolves under WSL at all, where `$HOME` is
`/home/<user>` on a different filesystem from the Windows user profile. The
Finviz Elite token lookup was silently failing to that second path.

**The layout predated any convention.** `~/market_data/`, `~/.tradekit/cache/`,
`~/Trade_Review/`, and `~/.falcon/falcon.db` are four different naming styles for
four kinds of data. Putting a regenerable provider cache under a dotdir in `$HOME`
also means it lands in backups, which is the opposite of what a cache wants.

**Ownership was invisible.** Nothing in the code distinguished paths tradekit
owns and may move freely from paths that are cross-repo contracts with the rest
of the Falcon suite. `~/.falcon/falcon.db` is written by falcon and only read
here (contract C1); the Trade_Review tree is shared with falcon-stats (C2).
Relocating either unilaterally breaks the suite.

## Decision

**Resolve all paths through a single module, `tradekit.paths`.** No other module
constructs a path from `Path.home()`. The exception is `~/.claude/...`, which
belongs to PAI and is not ours to place.

**Follow the XDG Base Directory specification.** Note that `~/.local/<app>` is
*not* an XDG location — only `share`, `state`, `bin`, and `lib` live directly
under `~/.local`.

| Kind | Variable | Default | tradekit |
|---|---|---|---|
| Durable data | `$XDG_DATA_HOME` | `~/.local/share` | `~/.local/share/tradekit/` |
| Regenerable cache | `$XDG_CACHE_HOME` | `~/.cache` | `~/.cache/tradekit/` |
| Disposable state | `$XDG_STATE_HOME` | `~/.local/state` | `~/.local/state/tradekit/` |

**Make ownership explicit in the module,** separating paths tradekit owns from
shared suite contracts, and never relocate a contract path unilaterally.

**Resolve leniently, in a fixed order:** an explicit environment override, then
the XDG location if it exists, then any legacy location that exists, then the
XDG location as the thing to create. An existing legacy directory therefore
wins over a non-existent XDG one, so upgrading never strands data already on
disk. A fresh install gets XDG with no migration step.

## Consequences

Relocation is now a one-line change per path, and `$XDG_*` overrides work for
free — which is what makes a single WSL/Windows checkout viable, since each side
can point at its own data without touching source.

Existing installs keep using their current directories until the operator moves
them. That is deliberate, but it does mean two machines can sit on different
layouts indefinitely; `tradekit paths` (not yet built) would make that visible.

The falcon and Trade_Review paths accept both locations, so tradekit is
migration-neutral: falcon and falcon-stats can move independently and in any
order. The cost is that the fallback chains must stay until the whole suite has
moved, and `docs/falcon-suite-compatibility.md` has to record both locations
for C1 and C2 in the meantime.

## Migration

Optional — nothing breaks if skipped.

```bash
mkdir -p ~/.local/share ~/.cache
mv ~/market_data      ~/.local/share/tradekit
mv ~/.tradekit/cache  ~/.cache/tradekit    && rmdir ~/.tradekit
mv ~/Trade_Review     ~/.local/share/trade-review   # coordinate with falcon-stats
```

`falcon.db` is falcon's to move. Until it does, tradekit finds it where it lies.
