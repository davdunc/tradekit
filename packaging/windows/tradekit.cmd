@echo off
rem Run the WSL-installed tradekit from Windows (cmd / PowerShell).
rem A login shell is used so ~/.local/bin and PAI_DIR from your WSL profile apply.
rem Set TRADEKIT_WSL_DISTRO to pick a distro; otherwise the WSL default is used.
setlocal
if defined TRADEKIT_WSL_DISTRO (
    wsl.exe -d "%TRADEKIT_WSL_DISTRO%" --exec bash -lc "exec tradekit \"$@\"" tradekit %*
) else (
    wsl.exe --exec bash -lc "exec tradekit \"$@\"" tradekit %*
)
exit /b %ERRORLEVEL%
