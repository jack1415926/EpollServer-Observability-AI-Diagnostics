"""Embed only the three known dashboard assets; no runtime filesystem serving."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
lines = ['#pragma once', '#include <string_view>', 'namespace epoll_monitor {',
         'struct WebAsset { std::string_view path, type, body; };',
         'inline constexpr WebAsset WebAssets[] = {']
for path, name, kind in [('/', 'index.html', 'text/html; charset=utf-8'),
                         ('/app.js', 'app.js', 'text/javascript; charset=utf-8'),
                         ('/style.css', 'style.css', 'text/css; charset=utf-8')]:
    body = (root / name).read_text()
    assert ')EPOLLWEB"' not in body
    lines.append(f'{{"{path}", "{kind}", R"EPOLLWEB({body})EPOLLWEB"}},')
lines.extend(['};', '}'])
Path(sys.argv[1]).write_text('\n'.join(lines) + '\n')
