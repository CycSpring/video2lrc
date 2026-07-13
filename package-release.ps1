[CmdletBinding()]
param(
    [string]$Version = "0.1.0",
    [string]$IsccPath = "",
    [string]$FfmpegLicensePath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = $PSScriptRoot
$DistRoot = Join-Path $ProjectRoot "dist\Video2LRC"
$GuiExecutable = Join-Path $DistRoot "Video2LRC.exe"
$CliExecutable = Join-Path $DistRoot "video2lrc-cli.exe"
$BundledFfmpeg = Join-Path $DistRoot "_internal\bin\ffmpeg.exe"
$BundledFfprobe = Join-Path $DistRoot "_internal\bin\ffprobe.exe"
$ReleaseRoot = Join-Path $ProjectRoot "release"
$StagingRoot = Join-Path $ReleaseRoot "staging"
$StagingApp = Join-Path $StagingRoot "Video2LRC"
$InstallerScript = Join-Path $ProjectRoot "installer\Video2LRC.iss"
$PortableArchive = Join-Path $ReleaseRoot "Video2LRC-v$Version-windows-x64-portable.zip"
$InstallerArtifact = Join-Path $ReleaseRoot "Video2LRC-v$Version-windows-x64-setup.exe"
$ChecksumArtifact = Join-Path $ReleaseRoot "SHA256SUMS.txt"
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Version must use semantic x.y.z form: $Version"
}

foreach ($requiredFile in @(
    $GuiExecutable,
    $CliExecutable,
    $BundledFfmpeg,
    $BundledFfprobe,
    $InstallerScript,
    (Join-Path $ProjectRoot "README.md"),
    (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md"),
    (Join-Path $ProjectRoot "requirements.lock.txt"),
    $Python
)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Required release input is missing: $requiredFile"
    }
}

$productVersion = (Get-Item -LiteralPath $GuiExecutable).VersionInfo.ProductVersion.Trim()
if ($productVersion -ne $Version) {
    throw "Release version $Version does not match Video2LRC.exe product version $productVersion."
}

if (-not $FfmpegLicensePath) {
    throw "Pass -FfmpegLicensePath for the exact bundled FFmpeg distribution."
}
$resolvedFfmpegLicense = (Resolve-Path -LiteralPath $FfmpegLicensePath -ErrorAction Stop).Path

function Resolve-Iscc {
    param([string]$RequestedPath)

    $candidates = @(
        $RequestedPath,
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { $_ }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }
    throw "Inno Setup 6 compiler was not found. Install JRSoftware.InnoSetup or pass -IsccPath."
}

function Copy-DistributionLicenses {
    param(
        [string]$SitePackages,
        [string]$DistributionPattern,
        [string]$DestinationRoot
    )

    $metadataDirectories = @(
        Get-ChildItem -LiteralPath $SitePackages -Directory `
            -Filter "$DistributionPattern-*.dist-info" -ErrorAction SilentlyContinue
    )
    foreach ($metadataDirectory in $metadataDirectories) {
        $licenseFiles = @(
            Get-ChildItem -LiteralPath $metadataDirectory.FullName -Recurse -File |
                Where-Object {
                    $_.Name -match '^(LICENSE|LICENCE|COPYING|NOTICE)' -or
                    $_.DirectoryName -match '[\\/]licenses?([\\/]|$)'
                }
        )
        foreach ($licenseFile in $licenseFiles) {
            $relativePath = $licenseFile.FullName.Substring(
                $metadataDirectory.FullName.Length
            ).TrimStart('\', '/')
            $destination = Join-Path `
                (Join-Path $DestinationRoot $metadataDirectory.Name) `
                $relativePath
            $destinationDirectory = Split-Path -Parent $destination
            New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
            Copy-Item -LiteralPath $licenseFile.FullName -Destination $destination -Force
        }
    }
}

$resolvedIscc = Resolve-Iscc -RequestedPath $IsccPath
$releaseFullPath = [System.IO.Path]::GetFullPath($ReleaseRoot)
$projectFullPath = [System.IO.Path]::GetFullPath($ProjectRoot)
if (-not $releaseFullPath.StartsWith($projectFullPath + [System.IO.Path]::DirectorySeparatorChar)) {
    throw "Release path escaped the project root: $releaseFullPath"
}

if (Test-Path -LiteralPath $ReleaseRoot) {
    Remove-Item -LiteralPath $ReleaseRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $StagingRoot -Force | Out-Null

try {
    Copy-Item -LiteralPath $DistRoot -Destination $StagingApp -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "README.md") `
        -Destination (Join-Path $StagingApp "README.md") -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") `
        -Destination (Join-Path $StagingApp "THIRD_PARTY_NOTICES.md") -Force
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "requirements.lock.txt") `
        -Destination (Join-Path $StagingApp "requirements.lock.txt") -Force

    $licenseRoot = Join-Path $StagingApp "licenses"
    New-Item -ItemType Directory -Path $licenseRoot -Force | Out-Null
    Copy-Item -LiteralPath $resolvedFfmpegLicense `
        -Destination (Join-Path $licenseRoot "FFmpeg-GPL-3.0.txt") -Force
    $ffmpegReadme = Join-Path (Split-Path -Parent $resolvedFfmpegLicense) "README.txt"
    if (Test-Path -LiteralPath $ffmpegReadme -PathType Leaf) {
        Copy-Item -LiteralPath $ffmpegReadme `
            -Destination (Join-Path $licenseRoot "FFmpeg-build-README.txt") -Force
    }

    $pythonBase = (& $Python -c "import sys; print(sys.base_prefix)" | Select-Object -Last 1).Trim()
    $pythonLicense = Join-Path $pythonBase "LICENSE.txt"
    if (Test-Path -LiteralPath $pythonLicense -PathType Leaf) {
        Copy-Item -LiteralPath $pythonLicense `
            -Destination (Join-Path $licenseRoot "Python-PSF-LICENSE.txt") -Force
    }

    $sitePackages = Join-Path $ProjectRoot ".venv\Lib\site-packages"
    $runtimeDistributions = @(
        "PySide6_Essentials",
        "shiboken6",
        "rapidocr",
        "onnxruntime",
        "opencv_python",
        "numpy",
        "pillow",
        "PyYAML",
        "RapidFuzz",
        "shapely",
        "tqdm",
        "requests",
        "charset_normalizer",
        "idna",
        "certifi",
        "urllib3",
        "flatbuffers",
        "protobuf",
        "omegaconf",
        "pyclipper",
        "antlr4_python3_runtime",
        "colorlog"
    )
    foreach ($distribution in $runtimeDistributions) {
        Copy-DistributionLicenses `
            -SitePackages $sitePackages `
            -DistributionPattern $distribution `
            -DestinationRoot $licenseRoot
    }

    $tar = Get-Command "tar.exe" -ErrorAction Stop
    & $tar.Source "-a" "-c" "-f" $PortableArchive "-C" $StagingRoot "Video2LRC"
    if ($LASTEXITCODE -ne 0) {
        throw "Portable archive creation failed with exit code $LASTEXITCODE."
    }

    & $resolvedIscc `
        "/Qp" `
        "/DMyAppVersion=$Version" `
        "/DSourceDir=$StagingApp" `
        "/DOutputDir=$ReleaseRoot" `
        $InstallerScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup compilation failed with exit code $LASTEXITCODE."
    }

    foreach ($artifact in @($PortableArchive, $InstallerArtifact)) {
        if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
            throw "Expected release artifact is missing: $artifact"
        }
    }

    $checksumLines = foreach ($artifact in @($InstallerArtifact, $PortableArchive)) {
        $hash = (Get-FileHash -LiteralPath $artifact -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $([System.IO.Path]::GetFileName($artifact))"
    }
    [System.IO.File]::WriteAllLines(
        $ChecksumArtifact,
        $checksumLines,
        [System.Text.UTF8Encoding]::new($false)
    )
} finally {
    if (Test-Path -LiteralPath $StagingRoot) {
        Remove-Item -LiteralPath $StagingRoot -Recurse -Force
    }
}

Get-Item -LiteralPath $InstallerArtifact, $PortableArchive, $ChecksumArtifact |
    Select-Object FullName, Length, LastWriteTime
