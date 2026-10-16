Dim WshShell, fso, rootDir, pythonwPath, cmd
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

rootDir = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonwPath = rootDir & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(pythonwPath) Then
    pythonwPath = "pythonw.exe"
End If

cmd = """" & pythonwPath & """ """ & rootDir & "\scripts\host_auth_helper.py"""
WshShell.CurrentDirectory = rootDir
WshShell.Run cmd, 0, False
