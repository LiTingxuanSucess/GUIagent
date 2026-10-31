"""读取最新的 add_customer 诊断 JSONL，打印各区域的 OCR 框。"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
from pathlib import Path

diag_dir = Path('logs/add_customer')
all_jsonl = sorted(diag_dir.glob('*.jsonl'))
if not all_jsonl:
    print('no jsonl found')
    sys.exit(1)

# 读取最新的诊断文件（按修改时间取最新）
jf = max(all_jsonl, key=lambda f: f.stat().st_mtime)
print(f'file: {jf}')
boxes = [json.loads(l) for l in jf.read_text(encoding='utf-8').splitlines() if l.strip()]
print(f'total boxes: {len(boxes)}')

print('\n--- 全部框（按 ny 排序）---')
for b in sorted(boxes, key=lambda x: x['ny']):
    print(f'  nx={b["nx"]:.3f} ny={b["ny"]:.3f}  text={b["text"]!r}')

print('\n--- 右侧区域 (nx > 0.60) ---')
for b in sorted(boxes, key=lambda x: x['ny']):
    if b['nx'] > 0.60:
        print(f'  nx={b["nx"]:.3f} ny={b["ny"]:.3f}  text={b["text"]!r}')
