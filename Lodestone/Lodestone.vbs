' Starts Lodestone with no console window.
'
' Prefers the desktop app (desktop\, C# + Avalonia). The .dll build runs on the
' installed .NET 8 runtime; it is tried before the .exe because this PC's
' antivirus refuses freshly built executables. Either way the engine used is
' lodestone.py in this folder, so edits here take effect without a rebuild.
' With no desktop build yet, it falls back to the old browser page.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
engine = " --engine """ & here & "\lodestone.py"""
dll = here & "\desktop\dist\dll\Lodestone.dll"
exe = here & "\desktop\dist\win-x64\Lodestone.exe"
' dotnet is on PATH only for programs started after .NET was installed, so
' look in its install folder first.
dotnet = sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\dotnet\dotnet.exe"
If Not fso.FileExists(dotnet) Then dotnet = "dotnet"

' An update that changed the desktop app's source leaves this flag; build
' the window before starting it so the new version is what opens.
flag = here & "\.lodestone-update\rebuild-desktop"
If fso.FileExists(flag) And fso.FileExists(here & "\desktop\build.py") And _
   (fso.FileExists(dotnet) Or dotnet = "dotnet") Then
  sh.Popup "Updating the Lodestone window - this takes a minute.", 3, "Lodestone", 64
  On Error Resume Next
  ' console Python in a hidden window, so dotnet's own console stays hidden too
  rc = sh.Run("cmd /c py """ & here & "\desktop\build.py"" --dll || python """ & here & "\desktop\build.py"" --dll", 0, True)
  If Err.Number = 0 And rc = 0 Then fso.DeleteFile flag
  Err.Clear
  On Error GoTo 0
End If

' The first start after a fresh download: no window built yet, but the .NET
' SDK is here - build it once, so double-clicking this is all it takes.
If Not fso.FileExists(dll) And Not fso.FileExists(exe) And fso.FileExists(here & "\desktop\build.py") And HasSdk() Then
  sh.Popup "Setting up Lodestone for the first time - this takes a minute.", 4, "Lodestone", 64
  On Error Resume Next
  rc = sh.Run("cmd /c py """ & here & "\desktop\build.py"" --dll || python """ & here & "\desktop\build.py"" --dll", 0, True)
  Err.Clear
  On Error GoTo 0
End If

On Error Resume Next
If fso.FileExists(dll) Then
  sh.Run """" & dotnet & """ """ & dll & """" & engine, 0, False
  If Err.Number = 0 Then WScript.Quit
  Err.Clear
End If
If fso.FileExists(exe) Then
  sh.Run """" & exe & """" & engine, 1, False
  If Err.Number = 0 Then WScript.Quit
  Err.Clear
End If
sh.Run "pythonw """ & here & "\lodestone.py""", 0, False
If Err.Number <> 0 Then
  Err.Clear
  sh.Run "cmd /c py """ & here & "\lodestone.py"" & pause", 1, False
End If

' True when a .NET SDK of version 8 or newer is installed in the usual place.
Function HasSdk()
  HasSdk = False
  sdk = sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\dotnet\sdk"
  If Not fso.FolderExists(sdk) Then Exit Function
  For Each f In fso.GetFolder(sdk).SubFolders
    major = Split(f.Name, ".")(0)
    If IsNumeric(major) Then
      If CInt(major) >= 8 Then HasSdk = True
    End If
  Next
End Function
