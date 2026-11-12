[Setup]
AppId=ZEN.SurfaceTilt.Workbench
AppName=ZEN AF Surface Tilt
AppVersion=2.3.0
DefaultDirName={localappdata}\Programs\ZEN Surface Tilt
DefaultGroupName=ZEN AF Surface Tilt
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableDirPage=yes
DisableProgramGroupPage=yes
WizardStyle=modern
OutputDir=.
OutputBaseFilename=ZEN_Surface_Tilt_Setup_V2.3.0
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=ZEN AF Surface Tilt

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\ZEN AF Surface Tilt"; Filename: "{app}\start_gui.bat"; WorkingDir: "{app}"
Name: "{userdesktop}\ZEN AF Surface Tilt"; Filename: "{app}\start_gui.bat"; WorkingDir: "{app}"

[Run]
Filename: "{app}\start_gui.bat"; Description: "Launch ZEN AF Surface Tilt"; Flags: shellexec nowait postinstall skipifsilent
