# tradekit Fedora packaging

This directory contains Fedora/RPM packaging artifacts for tradekit.

## Files

| File | Purpose |
|------|---------|
| `tradekit.spec` | RPM spec — `rpmbuild -ba` source. Targets Fedora 44+ (Python 3.14). |
| `tradekit.conf.example` | Default config skeleton, installed to `/etc/tradekit/`. |
| `tradekit-groups-daily.service` | systemd user service: runs `tradekit groups --diff auto`. |
| `tradekit-groups-daily.timer` | systemd user timer: 22:45 UTC daily (post US close). |

## Local build (Fedora or WSL Fedora)

```bash
# 1. Install build tooling
sudo dnf install -y rpm-build rpmdevtools rpm-build-python pyproject-rpm-macros

# 2. Set up rpmbuild tree (one-time)
rpmdev-setuptree

# 3. Create source tarball matching Source0 in the spec (for v0.1.0)
git archive --prefix=tradekit-0.1.0/ -o ~/rpmbuild/SOURCES/tradekit-0.1.0.tar.gz HEAD

# 4. Build
rpmbuild -ba packaging/tradekit.spec

# 5. Install the result
sudo dnf install ~/rpmbuild/RPMS/noarch/tradekit-0.1.0-1.fc44.noarch.rpm
```

Verify:
```bash
which tradekit              # → /usr/bin/tradekit
tradekit --help             # full command list
tradekit groups --help      # confirms Finviz Elite integration installed
ls /etc/tradekit/           # tradekit.conf.example present
ls /etc/bash_completion.d/  # tradekit completion present
```

## COPR build + distribution

[COPR](https://copr.fedorainfracloud.org/) is the right channel for distributing
this RPM since `finvizfinance` and `ta` aren't yet in official Fedora repos
(they're vendored — see `src/tradekit/_vendor/`).

### One-time setup

1. Create the COPR project at https://copr.fedorainfracloud.org/coprs/davdunc/tradekit/
   - Chroots: `fedora-44-x86_64` (and `fedora-rawhide-x86_64` if you want bleeding-edge)
   - Build options: enable network access (needed for `pyproject_buildrequires` if any pip-resolved deps slip through)

2. Install + configure `copr-cli`:
   ```bash
   sudo dnf install copr-cli
   # Get your token from https://copr.fedorainfracloud.org/api/ — paste into ~/.config/copr
   ```

### Build a release

```bash
# After tagging v0.1.0 on GitHub:
copr-cli build davdunc/tradekit \
    https://github.com/davdunc/tradekit/archive/v0.1.0.tar.gz
```

### Auto-build on git push (optional, requires .copr.yml)

When ready to automate, add `.copr.yml` at the repo root pointing the Webhook
build to `packaging/tradekit.spec`. COPR will rebuild on every push or tag.

### User install via COPR

```bash
sudo dnf copr enable davdunc/tradekit
sudo dnf install tradekit
```

## Why no `/etc/profile.d/tradekit.sh`?

Per Fedora Packaging Guidelines, applications shipped to `/usr/bin/` are
already on the default `PATH` for all login shells — adding a profile.d script
to manipulate PATH would be redundant and noisy. Tradekit follows that
convention:

- Binary: `/usr/bin/tradekit` (auto-on-PATH)
- Bash completion: `/etc/bash_completion.d/tradekit` (auto-loaded)
- User config: `~/.config/tradekit/tradekit.conf` (override `/etc/tradekit/`)
- Secrets: read from `$HOME/.env` or shell environment, never `/etc/`

## Vendored dependencies

`finvizfinance` (1.3.0) and `ta` (0.11.0) are vendored at
`src/tradekit/_vendor/` because they are not yet in Fedora repos. The spec
declares them as `Provides: bundled(python3-finvizfinance) = 1.3.0` etc. per
[FPG bundling rules](https://docs.fedoraproject.org/en-US/packaging-guidelines/#bundling).

When either lands in Fedora, drop the vendor copy and convert the bundling
declaration to a `Requires:` line.

## Systemd timer

The `tradekit-groups-daily.timer` is installed as a **user** unit (not system)
because it writes to `~/market_data/`. To activate after install:

```bash
systemctl --user daemon-reload
systemctl --user enable --now tradekit-groups-daily.timer
systemctl --user list-timers | grep tradekit       # confirm scheduled
```
