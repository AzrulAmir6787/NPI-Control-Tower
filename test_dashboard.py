"""Generate actual layout JS from app f-strings, validate syntax and run offline VM checks."""
import ast
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
TREE = ast.parse((ROOT/'app.py').read_text())
FN = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == 'render_executive_status')
def flatten(node):
    if isinstance(node, ast.Constant): return str(node.value)
    if isinstance(node, ast.JoinedStr): return ''.join(flatten(v) for v in node.values)
    if isinstance(node, ast.FormattedValue):
        expr = ast.unparse(node.value)
        return '"opaque-test-owner"' if expr == 'json.dumps(dashboard_layout_owner)' else 'opaque-test-owner' if expr == 'dashboard_layout_owner' else ''
    raise ValueError(type(node))
strings=[]
for node in ast.walk(FN):
    if isinstance(node, ast.Assign) and any(isinstance(v, ast.Name) and v.id=='floating_html' for v in node.targets): strings.append(flatten(node.value))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr=='html' and node.args and isinstance(node.args[0], ast.JoinedStr): strings.append(flatten(node.args[0]))
with tempfile.TemporaryDirectory() as tmp:
    scripts=[]
    for i,text in enumerate(strings):
        if '<script>' not in text: continue
        script=Path(tmp)/f'dashboard_{i}.js'
        script.write_text(text.split('<script>')[1].split('</script>')[0])
        subprocess.run(['node','--check',str(script)],check=True)
        scripts.append(script)
    assert len(scripts)==2
    print('PASS: actual dashboard and native installer JavaScript syntax',flush=True)
    subprocess.run(['node',str(ROOT/'test_dashboard.cjs'),str(scripts[0])],check=True)
    subprocess.run(['node',str(ROOT/'test_native_enquiry.cjs'),str(scripts[1])],check=True)
