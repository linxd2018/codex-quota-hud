Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
program = folder & "\release\CodexQuotaHUD\CodexQuotaHUD.exe"
If files.FileExists(program) Then
    shell.Run """" & program & """", 1, False
Else
    MsgBox "HUD program is missing. Build source or restore the complete release folder.", 48, "Codex Quota HUD"
End If
