@echo off
rem Grabs the Minehut dashboard login from Firefox and gives it to Lodestone (tools\minehut_login.py).
rem Log in at dashboard.minehut.com in Firefox first. Run again when Lodestone's Minehut actions fail with 401.
cd /d "%~dp0"
py tools\minehut_login.py
if errorlevel 9009 python tools\minehut_login.py
