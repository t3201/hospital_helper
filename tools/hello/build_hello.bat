@echo off
setlocal EnableExtensions
set JAVA_HOME=C:\Program Files\Java\jdk-21
set BT=D:\android-sdk\build-tools\34.0.0
set AJAR=D:\android-sdk\platforms\android-27\android.jar
set HERE=D:\Sources\hospital_helper\tools\hello
cd /d %HERE%
if not exist build\obj mkdir build\obj
if not exist build\tools mkdir build\tools

echo === STEP 1: javac (Hello) ===
"%JAVA_HOME%\bin\javac" -J-Duser.language=en -J-Duser.country=US -source 8 -target 8 -Xlint:-options -classpath "%AJAR%" -d build\obj src\Hello.java
echo javac_exit=%ERRORLEVEL%
if not exist build\obj\com\example\hello\Hello.class (echo JAVAC FAILED & exit /b 1)

echo === STEP 1b: javac (AddDex helper) ===
"%JAVA_HOME%\bin\javac" -J-Duser.language=en -J-Duser.country=US -d build\tools tools\AddDex.java
echo addex_javac_exit=%ERRORLEVEL%

echo === STEP 2: d8 -> classes.dex ===
call "%BT%\d8.bat" --min-api 21 --lib "%AJAR%" --output build\ build\obj\com\example\hello\*.class
echo d8_exit=%ERRORLEVEL%
if not exist build\classes.dex (echo D8 FAILED & exit /b 1)

echo === STEP 3: aapt2 compile ===
"%BT%\aapt2" compile --dir res -o build\res.zip
echo aapt2_compile_exit=%ERRORLEVEL%

echo === STEP 4: aapt2 link (base.apk) ===
"%BT%\aapt2" link -o build\base.apk -I "%AJAR%" --manifest AndroidManifest.xml build\res.zip
echo aapt2_link_exit=%ERRORLEVEL%
if not exist build\base.apk (echo LINK FAILED & exit /b 1)

echo === STEP 5: inject classes.dex ===
"%JAVA_HOME%\bin\java" -cp build\tools AddDex build\base.apk build\classes.dex
echo addex_exit=%ERRORLEVEL%

echo === STEP 6: zipalign ===
"%BT%\zipalign.exe" -f 4 build\base.apk build\aligned.apk
echo zipalign_exit=%ERRORLEVEL%

echo === STEP 7: apksigner (debug keystore auto) ===
if not exist debug.keystore (
  "%JAVA_HOME%\bin\keytool" -genkeypair -v -keystore debug.keystore -alias androiddebugkey -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug,O=Android,C=US"
  echo keytool_exit=%ERRORLEVEL%
)
call "%BT%\apksigner.bat" sign --ks debug.keystore --ks-pass pass:android --ks-key-alias androiddebugkey --key-pass pass:android --out hello.apk build\aligned.apk
echo apksigner_exit=%ERRORLEVEL%

echo === VERIFY ===
call "%BT%\apksigner.bat" verify --print-certs hello.apk
echo verify_exit=%ERRORLEVEL%
echo === DONE ===
dir hello.apk
endlocal
