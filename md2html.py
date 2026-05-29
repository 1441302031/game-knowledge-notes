#!/usr/bin/env python3
"""
Markdown → Ghibli 风格 HTML 转换器（千与千寻 × 龙猫）
用法: python3 md2html.py <input.md> [output.html]
默认输出: /home/admin/.hermes/static-files/<name>.html
依赖: pip install markdown
"""

import markdown, re, sys, os, subprocess, tempfile

if len(sys.argv) < 2:
    print("用法: python3 md2html.py <input.md> [output.html]")
    print("默认输出到: /home/admin/.hermes/static-files/")
    sys.exit(1)

input_file = sys.argv[1]
STATIC_DIR = "/home/admin/.hermes/static-files"

if len(sys.argv) <= 2:
    output_file = os.path.join(STATIC_DIR, os.path.basename(input_file).replace('.md', '.html'))
    os.makedirs(STATIC_DIR, exist_ok=True)
else:
    output_file = sys.argv[2]

if not os.path.exists(input_file):
    print(f"错误: 文件不存在 {input_file}")
    sys.exit(1)

with open(input_file) as f:
    md_content = f.read()

extensions = ["fenced_code", "tables", "codehilite"]
body = markdown.markdown(md_content, extensions=extensions)

# ---- slug & toc ----

def make_slug(text):
    """生成统一的 URL 锚点 slug。保留中英文+数字，其他变连字符。"""
    keep = r'a-zA-Z0-9\u4e00-\u9fff\u3000-\u303f'
    slug = re.sub(f'[^{keep}]+', '-', text.lower())
    slug = slug.strip('-')
    slug = re.sub(r'-{2,}', '-', slug)
    return slug

def add_anchor_ids(html):
    """给标题添加 id 属性"""
    def replacer(m):
        level, title = m.group(1), m.group(2)
        inner = re.sub(r'<[^>]+>', '', title)
        slug = make_slug(inner)
        return f'<h{level} id="{slug}">{title}</h{level}>'
    return re.sub(r'<h(\d)[^>]*>(.*?)</h\1>', replacer, html)

def generate_toc(html):
    """从 HTML 标题生成目录"""
    items = []
    for m in re.finditer(r'<h(\d)[^>]*>(.*?)</h\1>', html, re.DOTALL):
        level = int(m.group(1))
        title = re.sub(r'<[^>]+>', '', m.group(2))
        slug = make_slug(title)
        if level <= 2:
            indent = (level - 1) * 12
            items.append(f'<li style="margin-left:{indent}px"><a href="#{slug}">{title}</a></li>')
    return '\n'.join(items)

body = add_anchor_ids(body)
toc_links = generate_toc(body)

# ---- template ----

html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{os.path.basename(input_file).replace('.md', '')}</title>
<link rel="stylesheet" href="/ghibli.css">
</head>
<body class="ghibli-body">
<nav class="ghibli-sidebar">
    <h2 class="ghibli-sidebar-title">📖 目录</h2>
    <ul>{toc_links}</ul>
</nav>
<main class="ghibli-main">
{body}
</main>
</body>
</html>"""

# ---- write ----

def write_with_sudo(path, content):
    """Write file, falling back to sudo if permission denied."""
    try:
        with open(path, "w") as f:
            f.write(content)
    except PermissionError:
        tf = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False)
        tf.write(content)
        tf.close()
        subprocess.run(['sudo', 'cp', tf.name, path], check=True)
        subprocess.run(['sudo', 'chmod', '644', path], check=True)
        os.unlink(tf.name)

write_with_sudo(output_file, html)

print(f"✅ HTML 生成成功: {output_file}")
print(f"   文件大小: {len(html):,} bytes")
print(f"   风格: Ghibli (千与千寻 × 龙猫)")
