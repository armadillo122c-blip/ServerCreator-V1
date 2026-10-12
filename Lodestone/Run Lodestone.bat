@echo off
rem Starts Lodestone. Uses the desktop app when it has been built
rem (py desktop\build.py --dll), otherwise the browser page.
cd /d "%~dp0"
set "DOTNET=%ProgramFiles%\dotnet\dotnet.exe"
if not exist "%DOTNET%" set "DOTNET=dotnet"
rem An update that changed the desktop app leaves this flag: rebuild first.
if exist ".lodestone-update\rebuild-desktop" if exist "desktop\build.py" (
  echo Updating the Lodestone window...
  py desktop\build.py --dll && del ".lodestone-update\rebuild-desktop"
)
if exist "desktop\dist\dll\Lodestone.dll" (
  "%DOTNET%" "desktop\dist\dll\Lodestone.dll" --engine "%~dp0lodestone.py" %*
  if not errorlevel 1 exit /b 0
)
py lodestone.py %*
if errorlevel 1 (
  echo.
  echo Lodestone could not start. Python 3.8+ must be installed and on PATH.
  pause
)
