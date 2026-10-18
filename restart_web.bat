@echo off
setlocal EnableExtensions
rem ================================================================
rem  restart_web.bat - hospital_helper web service one-click restart
rem  Usage: double-click, or in cmd:  restart_web.bat [port]  (default 8765)
rem  Steps:
rem    1) find listener(s) on the port via netstat -ano and taskkill /F them
rem    2) start  python -m app.web --port <port>  fully detached (no window),
rem       stdout/stderr appended to web_server.log
rem    3) poll http://127.0.0.1:<port>/api/jobs until OK (up to ~40 s)
rem  Note: only kills process(es) LISTENING on the target port.
rem        Exits 0 on success / 1 on failure; no infinite waits (no pause).
rem ================================================================
set "PORT=8765"
if not "%~1"=="" set "PORT=%~1"
set "PY=D:\Programs\anaconda\python.exe"
set "LOG=%~dp0web_server.log"

cd /d "%~dp0"

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    ping -n 13 127.0.0.1 >nul
    exit /b 1
)

echo [1/3] Looking for old web process listening on port %PORT% ...
set "OLDPIDS="
for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":%PORT% " ^| findstr /c:"LISTENING"') do set "OLDPIDS=%OLDPIDS% %%a"
if not defined OLDPIDS (
    echo       none found
    goto :launch
)
for %%p in (%OLDPIDS%) do (
    echo       killing PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
rem wait for the port to be released (max ~5 s, then force kill once)
set /a WAIT=0
:waitport
netstat -ano | findstr /c:":%PORT% " | findstr /c:"LISTENING" >nul
if errorlevel 1 goto :launch
set /a WAIT+=1
if %WAIT% GEQ 5 (
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr /c:":%PORT% " ^| findstr /c:"LISTENING"') do taskkill /F /PID %%a >nul 2>&1
    ping -n 2 127.0.0.1 >nul
    goto :launch
)
ping -n 2 127.0.0.1 >nul
goto :waitport

:launch
echo [2/3] Starting new web service (detached, log: %LOG%) ...
"%PY%" -c "import subprocess,sys,os;p=subprocess.Popen([sys.executable,'-m','app.web','--port',sys.argv[1]],stdout=open('web_server.log','a',encoding='utf-8',errors='replace'),stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=0x8|0x200,cwd=os.getcwd(),close_fds=True);print('  started pid',p.pid)" %PORT%
if errorlevel 1 (
    echo [ERROR] failed to spawn web service
    ping -n 13 127.0.0.1 >nul
    exit /b 1
)
ping -n 2 127.0.0.1 >nul

echo [3/3] Health check http://127.0.0.1:%PORT%/api/jobs ...
set /a HC=0
:health
set /a HC+=1
"%PY%" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:%PORT%/api/jobs',timeout=3).read()" >nul 2>&1
if not errorlevel 1 goto :ok
if %HC% GEQ 10 goto :fail
ping -n 2 127.0.0.1 >nul
goto :health

:ok
echo       OK! Local: http://127.0.0.1:%PORT%/   (LAN: check ipconfig IPv4)
ping -n 3 127.0.0.1 >nul
exit /b 0

:fail
echo       NOT up after retries. Check log: %LOG%
ping -n 13 127.0.0.1 >nul
exit /b 1
