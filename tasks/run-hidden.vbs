' Hidden-window launcher for the skywatch supervisor.
' Usage: wscript.exe run-hidden.vbs "C:\path\to\run.bat"
' Runs the target with no console window and forwards the exit code so Task
' Scheduler still sees pass/fail.

If WScript.Arguments.Count < 1 Then
  WScript.Quit 1
End If

Dim cmd, i
cmd = """" & WScript.Arguments(0) & """"
For i = 1 To WScript.Arguments.Count - 1
  cmd = cmd & " """ & WScript.Arguments(i) & """"
Next

Dim sh, rc
Set sh = CreateObject("WScript.Shell")
rc = sh.Run("cmd /c " & cmd, 0, True)   ' 0 = hidden, True = wait + return exit code
WScript.Quit rc
