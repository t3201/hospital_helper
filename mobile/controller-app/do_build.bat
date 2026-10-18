@echo off
set JAVA_HOME=C:\Program Files\Java\jdk-21
set PATH=%JAVA_HOME%\bin;%PATH%
cd /d D:\Sources\hospital_helper\mobile\controller-app
call gradlew.bat assembleDebug --console=plain
echo GRADLE_EXIT=%ERRORLEVEL%
