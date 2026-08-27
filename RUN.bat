@echo off
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "INPUT" mkdir "INPUT"
if not exist "OUTPUT" mkdir "OUTPUT"

where py >nul 2>&1
if errorlevel 1 goto try_python
py -3 -c "import sys" >nul 2>&1
if errorlevel 1 goto try_python
py -3 "video_banner_overlay.py" %*
set "APP_EXIT=%ERRORLEVEL%"
goto finish

:try_python
where python >nul 2>&1
if errorlevel 1 goto no_python
python -c "import sys" >nul 2>&1
if errorlevel 1 goto no_python
python "video_banner_overlay.py" %*
set "APP_EXIT=%ERRORLEVEL%"
goto finish

:no_python
echo.
echo ERROR: Python was not found.
echo Install Python from https://www.python.org/downloads/windows/
echo Enable the option: Add python.exe to PATH
set "APP_EXIT=1"

:finish
echo.
if "%APP_EXIT%"=="0" goto success
echo Finished with an error. See the message above.
goto wait_for_key

:success
echo Finished successfully.

:wait_for_key
echo.
pause
exit /b %APP_EXIT%
