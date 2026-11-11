"""Derive the Odoo build from the maintained shared Qt/PyInstaller spec."""
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parent
shared=root.parent/'weighbridge'
spec=(shared/'packaging/megasanj.spec').read_text(encoding='utf-8')
spec=spec.replace('project_dir = Path(SPECPATH).parent','project_dir = Path('+repr(str(shared))+')')
spec=spec.replace('[str(project_dir / "packaging" / "launcher.py")]', '['+repr(str(root/'launcher.py'))+']')
spec=spec.replace('pathex=[str(project_dir / "src")]', 'pathex=[str(project_dir / "src"), '+repr(str(root/'src'))+']')
spec=spec.replace('name="MegaSanj"','name="MegaSanjOdoo"')
spec=spec.replace('icon=[str(project_dir / "packaging" / "megasanj.ico")]', 'icon=['+repr(str(root/'src/megasanj_odoo/assets/icon.ico'))+']')
spec=spec.replace('datas=datas,','datas=datas + [('+repr(str(root/'src/megasanj_odoo/assets'))+', "megasanj_odoo/assets")],')
target=root/'build/odoo.spec'
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(spec,encoding='utf-8')
subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--distpath',str(Path(sys.argv[1]).resolve() if len(sys.argv)>1 else root/'release'),
                '--workpath',str(root/'build/work'),str(target)],check=True)
