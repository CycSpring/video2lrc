#ifndef MyAppVersion
  #define MyAppVersion "0.1.1"
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\Video2LRC"
#endif

#ifndef OutputDir
  #define OutputDir "..\release"
#endif

#define MyAppName "Video2LRC"
#define MyAppExeName "Video2LRC.exe"
#define ProjectUrl "https://github.com/CycSpring/video2lrc"

[Setup]
AppId={{2E31B2DF-1961-4C78-88B8-329AFB4FA049}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=CycSpring
AppPublisherURL={#ProjectUrl}
AppSupportURL={#ProjectUrl}/issues
AppUpdatesURL={#ProjectUrl}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDir}
OutputBaseFilename=Video2LRC-v{#MyAppVersion}-windows-x64-setup
SetupIconFile=..\assets\video2lrc.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany=CycSpring
VersionInfoDescription=Video2LRC Windows installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Languages]
Name: "chinesesimp"; MessagesFile: "languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent
