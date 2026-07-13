[CmdletBinding()]
param(
    [switch]$BundleFFmpeg,
    [string]$FfmpegPath = "",
    [string]$FfprobePath = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = $PSScriptRoot
$RequirementsFile = Join-Path $ProjectRoot "requirements.lock.txt"
$SpecFile = Join-Path $ProjectRoot "video2lrc.spec"
$VersionInfoFile = Join-Path $ProjectRoot "packaging\version_info.txt"
$IconPngFile = Join-Path $ProjectRoot "assets\video2lrc-icon.png"
$IconIcoFile = Join-Path $ProjectRoot "assets\video2lrc.ico"
$DistRoot = Join-Path $ProjectRoot "dist\Video2LRC"
$InternalRoot = Join-Path $DistRoot "_internal"
$GuiSmokePath = Join-Path $ProjectRoot "build\gui-smoke.png"
$ExpectedFileVersion = "0.1.0.0"

function Resolve-PythonCommand {
    param([string]$RequestedPython)

    if ($RequestedPython) {
        $resolved = Resolve-Path -LiteralPath $RequestedPython -ErrorAction Stop
        return @($resolved.Path)
    }

    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return @((Resolve-Path -LiteralPath $venvPython).Path)
    }

    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $launcher) {
        return @($launcher.Source, "-3.11")
    }

    $systemPython = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($null -ne $systemPython) {
        return @($systemPython.Source)
    }

    throw "Python 3.11 was not found. Install it or pass -Python with an executable path."
}

$PythonCommand = @(Resolve-PythonCommand -RequestedPython $Python)
$PythonExecutable = $PythonCommand[0]
$PythonPrefix = @($PythonCommand | Select-Object -Skip 1)

function Invoke-CheckedPython {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $commandArguments = @($PythonPrefix) + $Arguments
    & $PythonExecutable @commandArguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Resolve-MediaTool {
    param(
        [string]$RequestedPath,
        [string]$EnvironmentName,
        [string]$ExecutableName
    )

    $candidate = $RequestedPath
    if (-not $candidate) {
        $candidate = [Environment]::GetEnvironmentVariable($EnvironmentName)
    }
    if (-not $candidate) {
        $command = Get-Command $ExecutableName -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            $candidate = $command.Source
        }
    }
    if (-not $candidate) {
        throw "Cannot bundle $ExecutableName because it was not found. Pass its path or set $EnvironmentName."
    }

    if (Test-Path -LiteralPath $candidate -PathType Container) {
        $candidate = Join-Path $candidate $ExecutableName
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "$ExecutableName does not exist: $candidate"
    }
    $resolved = (Resolve-Path -LiteralPath $candidate).Path
    if ([System.IO.Path]::GetFileName($resolved) -ine $ExecutableName) {
        throw "Expected $ExecutableName, got: $resolved"
    }
    return $resolved
}

foreach ($requiredSource in @(
    $RequirementsFile,
    $SpecFile,
    (Join-Path $ProjectRoot "gui.py"),
    (Join-Path $ProjectRoot "main.py"),
    (Join-Path $ProjectRoot "packaging\hooks\hook-rapidocr.py"),
    $VersionInfoFile,
    $IconPngFile,
    $IconIcoFile
)) {
    if (-not (Test-Path -LiteralPath $requiredSource -PathType Leaf)) {
        throw "Required build source is missing: $requiredSource"
    }
}

$versionArguments = @($PythonPrefix) + @(
    "-c",
    "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}'); raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
)
$pythonVersion = (& $PythonExecutable @versionArguments | Select-Object -Last 1)
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.11") {
    throw "This build requires Python 3.11; detected $pythonVersion."
}
Write-Host "Using Python $pythonVersion at $PythonExecutable"

$modifiedEnvironmentNames = @(
    "VIDEO2LRC_FFMPEG",
    "VIDEO2LRC_FFPROBE",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "QT_QPA_PLATFORM"
)
$originalEnvironment = @{}
foreach ($environmentName in $modifiedEnvironmentNames) {
    $originalEnvironment[$environmentName] = `
        [Environment]::GetEnvironmentVariable($environmentName, "Process")
}

try {
    if ($BundleFFmpeg) {
        $resolvedFfmpeg = Resolve-MediaTool `
            -RequestedPath $FfmpegPath `
            -EnvironmentName "VIDEO2LRC_FFMPEG" `
            -ExecutableName "ffmpeg.exe"
        $resolvedFfprobe = Resolve-MediaTool `
            -RequestedPath $FfprobePath `
            -EnvironmentName "VIDEO2LRC_FFPROBE" `
            -ExecutableName "ffprobe.exe"
        [Environment]::SetEnvironmentVariable(
            "VIDEO2LRC_FFMPEG", $resolvedFfmpeg, "Process"
        )
        [Environment]::SetEnvironmentVariable(
            "VIDEO2LRC_FFPROBE", $resolvedFfprobe, "Process"
        )
        Write-Host "Bundling FFmpeg from $resolvedFfmpeg"
    } else {
        # Keep the default package thin even if the caller's shell has stale values.
        [Environment]::SetEnvironmentVariable("VIDEO2LRC_FFMPEG", $null, "Process")
        [Environment]::SetEnvironmentVariable("VIDEO2LRC_FFPROBE", $null, "Process")
        Write-Host "Building thin package without FFmpeg."
    }

    [Environment]::SetEnvironmentVariable(
        "PIP_DISABLE_PIP_VERSION_CHECK", "1", "Process"
    )
    [Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "Process")
    [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")

    Invoke-CheckedPython `
        -Description "Build dependency installation" `
        -Arguments @(
            "-m", "pip", "install", "--no-input",
            "-r", $RequirementsFile
        )

    Invoke-CheckedPython `
        -Description "Test suite" `
        -Arguments @("-m", "pytest", "-q")

    Invoke-CheckedPython `
        -Description "PyInstaller build" `
        -Arguments @(
            "-m", "PyInstaller",
            "--noconfirm",
            "--clean",
            "--distpath", (Join-Path $ProjectRoot "dist"),
            "--workpath", (Join-Path $ProjectRoot "build"),
            $SpecFile
        )

    $guiArtifact = Join-Path $DistRoot "Video2LRC.exe"
    $cliArtifact = Join-Path $DistRoot "video2lrc-cli.exe"
    $requiredArtifacts = @(
        $guiArtifact,
        $cliArtifact,
        (Join-Path $InternalRoot "rapidocr\config.yaml"),
        (Join-Path $InternalRoot "rapidocr\default_models.yaml"),
        (Join-Path $InternalRoot "assets\video2lrc-icon.png")
    )
    foreach ($requiredArtifact in $requiredArtifacts) {
        if (-not (Test-Path -LiteralPath $requiredArtifact -PathType Leaf)) {
            throw "Build artifact is missing: $requiredArtifact"
        }
    }

    $rapidOcrModels = @(
        Get-ChildItem -LiteralPath (Join-Path $InternalRoot "rapidocr\models") `
            -Filter "*.onnx" -File -ErrorAction SilentlyContinue
    )
    if ($rapidOcrModels.Count -lt 3) {
        throw "RapidOCR ONNX models are missing; expected at least 3, found $($rapidOcrModels.Count)."
    }

    $onnxRuntimeDll = Get-ChildItem -LiteralPath $DistRoot -Recurse -File `
        -Filter "onnxruntime.dll" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $onnxRuntimeDll) {
        throw "ONNX Runtime native library is missing from the build."
    }

    $qtPlatformPlugin = Get-ChildItem -LiteralPath $DistRoot -Recurse -File `
        -Filter "qwindows.dll" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $qtPlatformPlugin) {
        throw "Qt Windows platform plugin is missing from the build."
    }

    if ($BundleFFmpeg) {
        foreach ($mediaArtifact in @("ffmpeg.exe", "ffprobe.exe")) {
            $artifactPath = Join-Path (Join-Path $InternalRoot "bin") $mediaArtifact
            if (-not (Test-Path -LiteralPath $artifactPath -PathType Leaf)) {
                throw "Bundled media tool is missing: $artifactPath"
            }
        }
    }

    foreach ($executableArtifact in @($guiArtifact, $cliArtifact)) {
        $fileVersion = (Get-Item -LiteralPath $executableArtifact).VersionInfo.FileVersion
        if ($fileVersion -ne $ExpectedFileVersion) {
            throw "Unexpected file version for ${executableArtifact}: $fileVersion"
        }
    }

    Write-Host "Running frozen CLI smoke test."
    & $cliArtifact "--help" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Frozen CLI smoke test failed with exit code $LASTEXITCODE."
    }

    Write-Host "Running frozen GUI screenshot smoke test."
    [Environment]::SetEnvironmentVariable("QT_QPA_PLATFORM", "offscreen", "Process")
    Remove-Item -LiteralPath $GuiSmokePath -Force -ErrorAction SilentlyContinue
    $guiSmokeProcess = $null
    try {
        $guiSmokeProcess = Start-Process `
            -FilePath $guiArtifact `
            -ArgumentList @(
                "--screenshot", ('"{0}"' -f $GuiSmokePath),
                "--size", "720x600"
            ) `
            -WindowStyle Hidden `
            -PassThru
        if (-not $guiSmokeProcess.WaitForExit(30000)) {
            Stop-Process -Id $guiSmokeProcess.Id -Force -ErrorAction SilentlyContinue
            throw "Frozen GUI screenshot smoke test timed out."
        }
        if ($guiSmokeProcess.ExitCode -ne 0) {
            throw "Frozen GUI screenshot smoke test failed with exit code $($guiSmokeProcess.ExitCode)."
        }
        if (
            -not (Test-Path -LiteralPath $GuiSmokePath -PathType Leaf) -or
            (Get-Item -LiteralPath $GuiSmokePath).Length -le 0
        ) {
            throw "Frozen GUI screenshot smoke test did not produce an image."
        }
    } finally {
        Remove-Item -LiteralPath $GuiSmokePath -Force -ErrorAction SilentlyContinue
    }

    Write-Host "Build complete: $DistRoot"
} finally {
    foreach ($environmentName in $modifiedEnvironmentNames) {
        [Environment]::SetEnvironmentVariable(
            $environmentName,
            $originalEnvironment[$environmentName],
            "Process"
        )
    }
}
