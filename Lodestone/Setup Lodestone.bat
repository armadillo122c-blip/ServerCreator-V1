@echo off
rem One-click setup for Lodestone on Windows.
rem   1. checks for Python 3 and the .NET 8 SDK, and offers to install them with winget
rem   2. builds the desktop app (desktop\build.py --dll)
rem   3. puts Lodestone on the Desktop and in the Start menu
rem   4. starts it
rem Safe to run again at any time - it only does what is missing, and rebuilds the app.
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Lodestone setup
echo.
echo   Lodestone setup
echo   ===============
echo.

rem ---------------------------------------------------------------- Lodestone must not be running
set "RUNNING="
tasklist /fi "imagename eq dotnet.exe" /v 2>nul | find /i "Lodestone" >nul && set "RUNNING=1"
tasklist /fi "imagename eq Lodestone.exe" 2>nul | find /i "Lodestone.exe" >nul && set "RUNNING=1"
if defined RUNNING (
  echo   Lodestone is open. Close it first ^(and choose "Stop server and quit" or
  echo   "Keep it running"^), then run this again.
  echo.
  pause
  exit /b 1
)

rem ---------------------------------------------------------------- Python
call :find_python
if not defined PY (
  echo   [1/4] Python 3 is not installed.
  call :ask "        Install Python 3.12 now with winget?" || goto :no_python
  winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
  call :find_python
)
if not defined PY goto :no_python
echo   [1/4] Python - ok
"!PY!" --version

rem ---------------------------------------------------------------- .NET SDK
call :find_dotnet
if not defined DOTNET_SDK (
  echo   [2/4] The .NET 8 SDK is not installed ^(it builds the Lodestone window^).
  call :ask "        Install it now with winget?" || goto :no_dotnet
  winget install -e --id Microsoft.DotNet.SDK.8 --accept-package-agreements --accept-source-agreements
  call :find_dotnet
)
if not defined DOTNET_SDK goto :no_dotnet
echo   [2/4] .NET SDK !DOTNET_SDK! - ok

rem ---------------------------------------------------------------- build
echo   [3/4] Building the Lodestone window - this takes a minute the first time...
set "PATH=%ProgramFiles%\dotnet;%PATH%"
"!PY!" "desktop\build.py" --dll
if errorlevel 1 (
  echo.
  echo   The build failed - the lines above say why. Lodestone still works as a
  echo   browser page: double-click Lodestone.vbs.
  echo.
  pause
  exit /b 1
)
if exist ".lodestone-update\rebuild-desktop" del ".lodestone-update\rebuild-desktop"

rem ---------------------------------------------------------------- shortcuts
echo   [4/4] Adding Lodestone to the Desktop and the Start menu
call :shortcut Desktop
call :shortcut Programs

echo.
echo   Done. Starting Lodestone...
echo.
start "" wscript.exe "%~dp0Lodestone.vbs"
timeout /t 3 >nul
exit /b 0

rem ================================================================ helpers

:shortcut
rem A shortcut that starts Lodestone.vbs through wscript, with the Lodestone icon.
set "HERE=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$w=New-Object -ComObject WScript.Shell; $d=[Environment]::GetFolderPath('%~1'); $l=$w.CreateShortcut((Join-Path $d 'Lodestone.lnk')); $l.TargetPath=(Join-Path $env:SystemRoot 'System32\wscript.exe'); $l.Arguments=[char]34 + $env:HERE + 'Lodestone.vbs' + [char]34; $l.WorkingDirectory=$env:HERE; $l.IconLocation=$env:HERE + 'Lodestone.ico'; $l.Description='Lodestone - Minecraft server manager'; $l.Save()" >nul 2>&1
exit /b 0

:find_python
set "PY="
for %%c in (py python) do (
  if not defined PY (
    where %%c >nul 2>&1 && (
      %%c -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1 && for /f "delims=" %%p in ('where %%c') do if not defined PY set "PY=%%p"
    )
  )
)
rem winget installs for this user only, and PATH in this window is from before
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"
if not defined PY for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%d\python.exe" set "PY=%%d\python.exe"
exit /b 0

:find_dotnet
set "DOTNET_SDK="
set "DN=%ProgramFiles%\dotnet\dotnet.exe"
if not exist "!DN!" for /f "delims=" %%p in ('where dotnet 2^>nul') do set "DN=%%p"
if not exist "!DN!" exit /b 0
rem the outer quotes are eaten by cmd /c, which keeps the inner ones around the path
for /f "tokens=1" %%v in ('""!DN!" --list-sdks 2^>nul"') do (
  for /f "tokens=1 delims=." %%m in ("%%v") do if %%m GEQ 8 set "DOTNET_SDK=%%v"
)
exit /b 0

:ask
set "ANSWER="
set /p "ANSWER=%~1 [Y/n] "
if /i "!ANSWER!"=="n" exit /b 1
if /i "!ANSWER!"=="no" exit /b 1
exit /b 0

:no_python
echo.
echo   Lodestone needs Python 3.8 or newer. Get it from https://www.python.org/downloads/
echo   ^(tick "Add python.exe to PATH"^), then run this again.
echo.
pause
exit /b 1

:no_dotnet
echo.
echo   Without the .NET SDK Lodestone runs as a browser page instead - double-click
echo   Lodestone.vbs. For the desktop app, install the .NET 8 SDK from
echo   https://dotnet.microsoft.com/download/dotnet/8.0 and run this again.
echo.
pause
exit /b 1
