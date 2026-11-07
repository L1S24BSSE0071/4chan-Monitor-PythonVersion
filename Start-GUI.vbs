Option Explicit

Dim fso, shell, scriptPath, command
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptPath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "4chan-Monitor-GUI.pyw")
If Not fso.FileExists(scriptPath) Then
    MsgBox "Could not find the GUI application:" & vbCrLf & scriptPath, vbCritical, "4chan Thread Monitor"
    WScript.Quit 1
End If

shell.CurrentDirectory = fso.GetParentFolderName(scriptPath)
command = "pythonw.exe """ & scriptPath & """"
On Error Resume Next
shell.Run command, 0, False
If Err.Number <> 0 Then
    Err.Clear
    shell.Run "pyw.exe -3 """ & scriptPath & """", 0, False
    If Err.Number <> 0 Then
        MsgBox "Could not find Python's windowed runtime (pythonw.exe or pyw.exe)." & vbCrLf & _
            "Install Python for Windows, then run the GUI with pyw -3." & vbCrLf & vbCrLf & _
            Err.Description, vbCritical, "4chan Thread Monitor"
        WScript.Quit 1
    End If
End If
On Error GoTo 0
