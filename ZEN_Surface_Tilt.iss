[Setup]
AppId=ZEN.SurfaceTilt.Workbench
AppName=LSM测量工作站
AppVersion=2.8.0
DefaultDirName={localappdata}\Programs\ZEN Surface Tilt
DefaultGroupName=LSM测量工作站
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableDirPage=yes
DisableProgramGroupPage=yes
WizardStyle=modern
OutputDir=.
OutputBaseFilename=LSM_Measurement_Setup_V2.8.0
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=LSM测量工作站

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{userprograms}\ZEN AF Surface Tilt.lnk"
Type: files; Name: "{userdesktop}\ZEN AF Surface Tilt.lnk"

[Icons]
Name: "{userprograms}\LSM测量工作站"; Filename: "{app}\start_gui.bat"; WorkingDir: "{app}"
Name: "{userdesktop}\LSM测量工作站"; Filename: "{app}\start_gui.bat"; WorkingDir: "{app}"

[Run]
Filename: "{app}\start_gui.bat"; Description: "启动 LSM测量工作站"; Flags: shellexec nowait postinstall skipifsilent
