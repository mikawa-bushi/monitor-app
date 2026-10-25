<#
.SYNOPSIS
    monitor-app を Windows の起動時(またはサインイン時)に自動で開始するタスクを登録する。

.DESCRIPTION
    同じフォルダの start-monitor.bat をタスク スケジューラに登録する
    (「monitor-app deploy windows」で生成)。

    既定      : PC の起動時に、このスクリプトを実行したユーザーの権限(管理者の権限は
                使わない)で開始する。サインインしていなくても動き、パスワードは保存しない。
                登録には、管理者として実行した PowerShell が必要。
    -AtLogon  : 今のユーザーがサインインしたときに開始する(管理者権限は不要)。
    -AsSystem : PC の起動時に SYSTEM アカウントで開始する(以前の既定。おすすめしない)。
                プロジェクトのフォルダや Python を一般のユーザーが書き換えられると、
                その人が SYSTEM の権限でプログラムを実行できてしまう。
    -Uninstall: 登録したタスクを削除する。

    実行時間の上限は設けない(既定の 72 時間で止めない)。タスクを開始できなかったときは
    1 分おきに 3 回まで再試行する(タスク スケジューラは、開始したサーバーが途中で
    異常終了しても再起動しない。止まったときは logs\monitor.log を確かめる)。
    プロジェクトはローカルのドライブに置くこと(サインインせずに動くタスクからは
    ネットワーク ドライブが見えない)。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -AtLogon
#>
[CmdletBinding()]
param(
    [switch]$AtLogon,
    [switch]$AsSystem,
    [switch]$Uninstall,
    [string]$TaskName = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
if (-not $TaskName) {
    $TaskName = 'monitor-app (' + (Split-Path -Leaf $here) + ')'
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "タスク「$TaskName」を削除しました。"
    exit 0
}

$bat = Join-Path $here 'start-monitor.bat'
if (-not (Test-Path -LiteralPath $bat)) {
    Write-Host "start-monitor.bat が見つかりません: $bat" -ForegroundColor Red
    exit 1
}

# 一般のユーザー全員(Everyone・Authenticated Users・Users)が書き換えられるか。
# 書き換えられると、その人が書いたプログラム(config.py など)がタスクの権限で動く。
function Test-WritableByEveryone([string]$Path) {
    $broad = @('S-1-1-0', 'S-1-5-11', 'S-1-5-32-545')
    # WriteData・AppendData と、汎用の書き込み・すべての権限
    $writeBits = 0x2 -bor 0x4 -bor 0x40000000 -bor 0x10000000
    try {
        foreach ($ace in (Get-Acl -LiteralPath $Path).Access) {
            if ($ace.AccessControlType -ne 'Allow') { continue }
            try {
                $sid = $ace.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            } catch {
                continue
            }
            if (($broad -contains $sid) -and (([int]$ace.FileSystemRights -band $writeBits) -ne 0)) {
                return $true
            }
        }
    } catch {
        return $false
    }
    return $false
}

if (-not $AtLogon) {
    $exposed = @($here, $bat, (Join-Path $here 'config.py')) |
        Where-Object { (Test-Path -LiteralPath $_) -and (Test-WritableByEveryone $_) }
    if ($exposed) {
        Write-Host '注意: 次のフォルダ・ファイルは、この PC のすべてのユーザーが書き換えられます。' -ForegroundColor Yellow
        $exposed | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
        Write-Host '  書き換えた人のプログラムがタスクの権限で動くため、ほかのユーザーの書き込みを外してください' -ForegroundColor Yellow
        Write-Host '  (エクスプローラーでフォルダのプロパティ → セキュリティ、または icacls)。' -ForegroundColor Yellow
    }
}

# cmd.exe /c は、引用符の中に & ( ) などがあると最初と最後の引用符を外して解釈するため、
# 引用符を二重にして渡す(「R&D」などを含むフォルダでも動く)。
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ""' + $bat + '""') -WorkingDirectory $here
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if ($AtLogon) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
} else {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $role = [Security.Principal.WindowsBuiltInRole]::Administrator
    if (-not (New-Object Security.Principal.WindowsPrincipal $identity).IsInRole($role)) {
        Write-Host 'PC の起動時に開始するには、管理者として実行した PowerShell で実行してください。' -ForegroundColor Red
        Write-Host '(サインイン時の開始でよければ -AtLogon を付けて実行する。管理者権限は不要)'
        exit 1
    }
    $trigger = New-ScheduledTaskTrigger -AtStartup
    if ($AsSystem) {
        Write-Host '注意: SYSTEM アカウントで動かします。プロジェクトのフォルダと Python を、管理者だけが書き換えられるようにしてください。' -ForegroundColor Yellow
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    } else {
        # S4U: サインインしていなくても、このユーザーの権限(管理者の権限は使わない)で動く。
        # パスワードは保存しない(ネットワーク ドライブには接続できない)。
        $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType S4U -RunLevel Limited
    }
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description "monitor-app: $here" -Force | Out-Null

Write-Host "タスク「$TaskName」を登録しました(実行するユーザー: $($principal.UserId))。"
Write-Host "今すぐ開始する: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ('ログ: ' + (Join-Path $here 'logs\monitor.log'))
Write-Host "削除する: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Uninstall"
