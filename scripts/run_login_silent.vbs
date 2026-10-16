Dim WshShell, fso, rootDir, pythonwPath, service, cmd
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

rootDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonwPath = rootDir & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pythonwPath) Then
    pythonwPath = "pythonw.exe"
End If

service = "all"
If WScript.Arguments.Count > 0 Then
    service = WScript.Arguments(0)
End If

cmd = """" & pythonwPath & """ -m ptb_acquisition.playwright.login --service " & service

WshShell.CurrentDirectory = rootDir
WshShell.Run cmd, 0, False
