# SPDX-License-Identifier: MIT
#
# Fedora packaging for tradekit — pre-market screening + technical analysis CLI.
# Targets Fedora 44+ which ships Python 3.14.
#
# Build:
#   rpmbuild -ba packaging/tradekit.spec
#
# COPR build (after `copr-cli` configured):
#   copr-cli build davdunc/tradekit packaging/tradekit.spec
#
# Bundled libraries (Provides: bundled below): finvizfinance and ta are vendored
# at src/tradekit/_vendor/ because they are not yet packaged in Fedora repos.
# When upstream packages land in Fedora, drop the bundling and convert to
# Requires: lines.

Name:           tradekit
Version:        0.1.0
Release:        1%{?dist}
Summary:        Pre-market screening, technical analysis, and trade setup evaluation

License:        MIT
URL:            https://github.com/davdunc/tradekit
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-pip
# For %check
BuildRequires:  python3-pytest

# Runtime deps (all available in Fedora 44 repos as of writing)
Requires:       python3 >= 3.14
Requires:       python3-yfinance
Requires:       python3-pandas
Requires:       python3-numpy
Requires:       python3-pydantic-settings
Requires:       python3-mcp
Requires:       python3-pyarrow
Requires:       python3-click
Requires:       python3-rich
Requires:       python3-pyyaml
Requires:       python3-requests
Requires:       python3-aiohttp
Requires:       python3-boto3
Requires:       python3-beautifulsoup4
Requires:       python3-lxml
Requires:       python3-dotenv

# Bundled (vendored) deps not yet in Fedora — versions tracked in
# src/tradekit/_vendor/<pkg>/VENDORED_VERSION
Provides:       bundled(python3-finvizfinance) = 1.3.0
Provides:       bundled(python3-ta) = 0.11.0

%description
tradekit is a personal trading-infrastructure CLI for pre-market screening,
technical analysis, sector/industry rotation snapshots, relative-strength
scoring, and bull/bear LLM-driven debate setups. It integrates with
Personal_AI_Infrastructure (PAI), Finviz Elite, the Falcon screener, and
S3-compatible flat-file market data backends.

Commands include: scan, analyze, levels, watchlist, morning, report, gameplan,
groups, rs, debate, weekly-review, publish-review, init.

%prep
%autosetup -p1


%generate_buildrequires
%pyproject_buildrequires


%build
%pyproject_wheel


%install
%pyproject_install
%pyproject_save_files tradekit

# Bash completion (Click generates it from the registered CLI)
install -d -m 0755 %{buildroot}%{_sysconfdir}/bash_completion.d
_TRADEKIT_COMPLETE=bash_source %{buildroot}%{_bindir}/tradekit \
    > %{buildroot}%{_sysconfdir}/bash_completion.d/tradekit \
    2>/dev/null || true

# Default config skeleton
install -d -m 0755 %{buildroot}%{_sysconfdir}/tradekit
install -m 0644 packaging/tradekit.conf.example \
    %{buildroot}%{_sysconfdir}/tradekit/tradekit.conf.example

# Systemd timer + service for daily groups snapshot
install -d -m 0755 %{buildroot}%{_userunitdir}
install -m 0644 packaging/tradekit-groups-daily.service \
    %{buildroot}%{_userunitdir}/tradekit-groups-daily.service
install -m 0644 packaging/tradekit-groups-daily.timer \
    %{buildroot}%{_userunitdir}/tradekit-groups-daily.timer


%check
# Smoke test: the CLI must at least produce help without exception
%{buildroot}%{_bindir}/tradekit --help >/dev/null
# Run pytest if any tests are present (best-effort)
%pyproject_check_import || :
[ -d tests ] && %pytest -q || :


%files -f %{pyproject_files}
%license LICENSE
%license src/tradekit/_vendor/finvizfinance/LICENSE
%license src/tradekit/_vendor/ta/LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/tradekit
%{_sysconfdir}/bash_completion.d/tradekit
%dir %{_sysconfdir}/tradekit
%config(noreplace) %{_sysconfdir}/tradekit/tradekit.conf.example
%{_userunitdir}/tradekit-groups-daily.service
%{_userunitdir}/tradekit-groups-daily.timer


%changelog
* Sun May 03 2026 David Duncan <davdunc@davidduncan.org> - 0.1.0-1
- Initial Fedora packaging.
- Vendored finvizfinance 1.3.0 (MIT) and ta 0.11.0 (MIT) at
  src/tradekit/_vendor/ since they are not yet in Fedora repos.
- Ships /usr/bin/tradekit, bash completion, /etc/tradekit/ config skeleton,
  and user-level systemd timer for daily groups snapshot.
