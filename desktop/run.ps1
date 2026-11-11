$env:MEGASANJ_EDITION = 'odoo'
$env:PYTHONPATH = "$PSScriptRoot/src;$PSScriptRoot/../weighbridge/src"
& "$PSScriptRoot/../weighbridge/venv/Scripts/python.exe" "$PSScriptRoot/launcher.py"
