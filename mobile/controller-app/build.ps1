<#
  build.ps1 — 零 Gradle 打包（aapt2 + d8 + apksigner）。

  为什么不用 Gradle：本机只有 JDK21（无 Kotlin），且要“零第三方依赖”。Java 源码 + 系统 SDK
  工具链直接出 APK 最简。

  前置：Android SDK 的 build-tools 与 platform。本机当前未安装 → 用变量占位，装好后填上。

  用法示例：
    powershell -ExecutionPolicy Bypass -File build.ps1 -Sdk "D:\Android\sdk"
    powershell -ExecutionPolicy Bypass -File build.ps1 -Sdk $env:ANDROID_SDK_ROOT -BuildTools "34.0.0" -Platform "android-34"
#>
param(
    [string]$Sdk        = $env:ANDROID_SDK_ROOT,          # ← 占位：Android SDK 根目录
    [string]$BuildTools = "30.0.3",                       # 实测可用（含 dx.jar；34.0.0 的 d8 会 NPE）
    [string]$Platform   = "android-34",                   # platform 版本（android.jar 用于 javac）
    [string]$Keystore   = "$env:USERPROFILE\.android\debug.keystore",
    [string]$Alias      = "androiddebugkey",
    [string]$MinSdk     = "24",
    [string]$TargetSdk  = "30",
    [string]$AppName    = "hh-controller"
)

# 关键：本脚本大量调用 native 工具（aapt2/javac/d8/apksigner）。
# PS 5.1 会把 native 命令写到 stderr 的内容（哪怕只是 warning）包装成 ErrorRecord，
# 一旦 $ErrorActionPreference=Stop 就变成终止异常，根本走不到 $LASTEXITCODE 判断。
# 因此这里用 Continue，所有成败一律显式靠 $LASTEXITCODE 检查（脚本每个步骤都做了）。
$ErrorActionPreference = "Continue"

# ------------------------------------------------------------------ 路径
$Root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$Src    = Join-Path $Root "src"
$Res    = Join-Path $Root "res"
$Build  = Join-Path $Root "build"
$Gen    = Join-Path $Build "gen"
$Classes= Join-Path $Build "classes"
$DexOut = Join-Path $Build "dex"

if (-not $Sdk) {
    Write-Error "未指定 -Sdk。请安装 Android SDK 后传入 -Sdk <路径>（或设置 ANDROID_SDK_ROOT）。"
    exit 1
}

$Aapt2     = Join-Path $Sdk "build-tools\$BuildTools\aapt2.exe"
$D8        = Join-Path $Sdk "build-tools\$BuildTools\d8.bat"
$Zipalign  = Join-Path $Sdk "build-tools\$BuildTools\zipalign.exe"
$Apksigner = Join-Path $Sdk "build-tools\$BuildTools\apksigner.bat"
$AndroidJar= Join-Path $Sdk "platforms\$Platform\android.jar"

foreach ($t in @($Aapt2, $D8, $Zipalign, $Apksigner, $AndroidJar)) {
    if (-not (Test-Path $t)) { Write-Error "找不到工具：$t（检查 -BuildTools/-Platform 版本）"; exit 1 }
}

Write-Host "== 清理 build/ ==" -ForegroundColor Cyan
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force -Path $Gen, $Classes, $DexOut | Out-Null

# ------------------------------------------------------------------ 1) 资源编译 + 链接
Write-Host "== aapt2 compile ==" -ForegroundColor Cyan
& $Aapt2 compile --dir $Res -o (Join-Path $Build "res.zip")
if ($LASTEXITCODE -ne 0) { Write-Error "aapt2 compile 失败"; exit 1 }

Write-Host "== aapt2 link ==" -ForegroundColor Cyan
$LinkedApk = Join-Path $Build "app-linked.apk"
& $Aapt2 link `
    -o $LinkedApk `
    -I $AndroidJar `
    --manifest (Join-Path $Root "AndroidManifest.xml") `
    -R (Join-Path $Build "res.zip") `
    --java $Gen `
    --min-sdk-version $MinSdk `
    --target-sdk-version $TargetSdk `
    --version-code 1 --version-name "0.1.0-p0p2" `
    --auto-add-overlay
if ($LASTEXITCODE -ne 0) { Write-Error "aapt2 link 失败"; exit 1 }

# ------------------------------------------------------------------ 2) 编译 Java
Write-Host "== javac ==" -ForegroundColor Cyan
$sources = @()
$sources += Get-ChildItem -Recurse -Path $Src   -Filter *.java | ForEach-Object { $_.FullName }
$sources += Get-ChildItem -Recurse -Path $Gen   -Filter *.java | ForEach-Object { $_.FullName }
$srcList = Join-Path $Build "sources.txt"
# javac @file 每行一个路径
Set-Content -Path $srcList -Value $sources -Encoding ASCII

# 现代 JDK 用 -classpath android.jar 解析 android.*/org.json；-bootclasspath 兼容旧 JDK（可选）
# 注意：JDK21 下 -source 8/-target 8 会打 obsolete 警告（写 stderr），
# 但 PS 5.1 把 native 命令 stderr 当 ErrorRecord，配合 $ErrorActionPreference=Stop 会提前抛异常，
# 所以这里把 stderr 重定向到日志文件（2>file）而不是 2>$null，并靠 $LASTEXITCODE 判断成败。
$javacLog = Join-Path $Build "javac.log"
& javac -encoding UTF-8 -source 8 -target 8 -bootclasspath $AndroidJar -classpath $AndroidJar `
    -d $Classes "@$srcList" 2> $javacLog
if ($LASTEXITCODE -ne 0) {
    Write-Host "   第一次 javac 失败，改用 -classpath 重试…" -ForegroundColor Yellow
    & javac -encoding UTF-8 -source 8 -target 8 -classpath $AndroidJar -d $Classes "@$srcList" 2> $javacLog
    if ($LASTEXITCODE -ne 0) {
        if (Test-Path $javacLog) { Get-Content $javacLog | Write-Host }
        Write-Error "javac 失败"; exit 1
    }
}
if (Test-Path $javacLog) { Get-Content $javacLog | ForEach-Object { if ($_ -match 'error') { Write-Host $_ -ForegroundColor Yellow } } }

# ------------------------------------------------------------------ 3) dex
# 关键坑（本次调试实证）：
#  1) d8.bat 在缺少 SDK tools/find_java.bat 时会 `goto :EOF` 直接 RC=0 静默退出，
#     导致看起来"成功"但根本没生成 classes.dex。所以不能信任 d8.bat 的 RC。
#  2) JDK 21 的 javac 对匿名内部类的合成构造器会写出带 <no name> 的 MethodParameters
#     属性，d8/R8 的 JarClassFileReader$CreateMethodVisitor.visitParameter 遇到 null 名字
#     会 NPE（R8_8.2.2-dev / 30.0.3 / 28.0.3 都复现）。
#  3) 老一代 dexer "dx"（build-tools<=30）不读这个属性，能正常出 dex —— 本脚本统一用 dx。
#  4) dx 要求 class 文件的相对路径与类名（com/hh/.../X.class）一致，
#     且必须从"classes 根目录"启动，所以这里先 Set-Location 到 $Classes 再调 dx。
Write-Host "== dx (legacy dexer) ==" -ForegroundColor Cyan
$DxJar = Join-Path $Sdk "build-tools\$BuildTools\lib\dx.jar"
if (-not (Test-Path $DxJar)) {
    # 34.0.0 里没有 dx.jar；回退到任意装了 dx 的 build-tools
    $DxJar = $null
    Get-ChildItem -Path $Sdk -Directory -Filter "build-tools" -ErrorAction SilentlyContinue |
        ForEach-Object {
            if ($DxJar) { return }
            $c = Join-Path $_.FullName "lib\dx.jar"
            if (Test-Path $c) { $DxJar = $c }
        }
    if (-not $DxJar) { Write-Error "找不到 dx.jar（需要 build-tools<=30，例如 30.0.3）"; exit 1 }
    Write-Host "   dx.jar 回退到: $DxJar" -ForegroundColor Yellow
}
$javaExe = (Get-Command java).Source
$relativeClassFiles = Get-ChildItem -Recurse -Path $Classes -Filter *.class |
    ForEach-Object { $_.FullName.Substring($Classes.Length + 1) }
if ($relativeClassFiles.Count -eq 0) { Write-Error "没找到 .class 文件"; exit 1 }

Push-Location $Classes
try {
    $dxArgs = @('-cp', $DxJar, 'com.android.dx.command.dexer.Main',
                '--output', (Join-Path $DexOut 'classes.dex')) + $relativeClassFiles
    & $javaExe @dxArgs 2> (Join-Path $Build "dx.log")
} finally { Pop-Location }
if ($LASTEXITCODE -ne 0) {
    if (Test-Path (Join-Path $Build "dx.log")) { Get-Content (Join-Path $Build "dx.log") | Write-Host }
    Write-Error "dx 失败"; exit 1
}
$classesDex = Join-Path $DexOut "classes.dex"
if (-not (Test-Path $classesDex)) { Write-Error "dx 声称成功但没生成 classes.dex"; exit 1 }
Write-Host ("   classes.dex = " + (Get-Item $classesDex).Length + " bytes") -ForegroundColor Cyan

# ------------------------------------------------------------------ 4) 把 classes.dex 塞进 APK
# 说明：原脚本用 [System.IO.Compression.ZipFile] 往 zip 里加条目，但 -File 模式下
# Add-Type 加载后仍报 "Unable to find type [System.IO.Compression.ZipArchiveMode]"，
# 属于 PS 5.1 + JDK 环境怪癖。改用 JDK 自带的 `jar uf`（本质是 zip update），
# 从 classes.dex 所在目录执行 `jar uf <apk> classes.dex`，条目名即为 classes.dex。
Write-Host "== 打包 classes.dex 进 APK (jar uf) ==" -ForegroundColor Cyan
$PayloadApk = Join-Path $Build "app-payload.apk"
Copy-Item $LinkedApk $PayloadApk -Force
$dexPath = Join-Path $DexOut "classes.dex"
if (-not (Test-Path $dexPath)) { Write-Error "未生成 classes.dex"; exit 1 }
# JDK21 的 `jar uf` 会写出带 zip64 的 CEN 头，导致 aapt2 产物 APK 变 "Invalid CEN header"。
# 改用 Python zipfile 以追加方式写入 classes.dex（最稳，不重写已有条目）。
$py = & where.exe python 2>$null | Select-Object -First 1
if (-not $py) { $py = 'python' }
$pySrc = "import sys, zipfile`n" +
         "apk, dex = sys.argv[1], sys.argv[2]`n" +
         "with zipfile.ZipFile(apk, 'a', zipfile.ZIP_DEFLATED) as z:`n" +
         "    z.write(dex, 'classes.dex')`n" +
         "print('dex_added')"
$tmp = Join-Path $Build "add_dex.py"
Set-Content -Path $tmp -Value $pySrc -Encoding ASCII
& $py $tmp $PayloadApk (Join-Path $DexOut "classes.dex")
if ($LASTEXITCODE -ne 0) { Write-Error "classes.dex 未打进 APK"; exit 1 }

# ------------------------------------------------------------------ 5) zipalign
Write-Host "== zipalign ==" -ForegroundColor Cyan
$AlignedApk = Join-Path $Build "app-aligned.apk"
& $Zipalign -p -f 4 $PayloadApk $AlignedApk
if ($LASTEXITCODE -ne 0) { Write-Error "zipalign 失败"; exit 1 }

# ------------------------------------------------------------------ 6) 签名
if (-not (Test-Path $Keystore)) {
    Write-Host "== 生成 debug keystore ==" -ForegroundColor Cyan
    $ksDir = Split-Path -Parent $Keystore
    if (-not (Test-Path $ksDir)) { New-Item -ItemType Directory -Force -Path $ksDir | Out-Null }
    & keytool -genkeypair -keystore $Keystore -alias $Alias `
        -storepass android -keypass android -keyalg RSA -keysize 2048 -validity 10000 `
        -dname "CN=Android Debug,O=Android,C=US"
    if ($LASTEXITCODE -ne 0) { Write-Error "keytool 生成 keystore 失败"; exit 1 }
}

Write-Host "== apksigner sign ==" -ForegroundColor Cyan
$OutApk = Join-Path $Root "$AppName.apk"
& $Apksigner sign `
    --ks $Keystore --ks-key-alias $Alias `
    --ks-pass pass:android --key-pass pass:android `
    --out $OutApk $AlignedApk
if ($LASTEXITCODE -ne 0) { Write-Error "apksigner 失败"; exit 1 }

& $Apksigner verify --print-certs $OutApk | Out-Null

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "APK: $OutApk"
Write-Host "安装: adb -s <serial> install -r `"$OutApk`""
Write-Host "真机: MI 5X 0043d38b0504 / Redmi 476cf991"
