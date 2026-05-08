# Micro-Sniper 调试指南

## 常用调试命令

### 列出 AgentBay 上下文
```bash
poetry run python -c "
import asyncio
from config.settings import global_settings
from agentbay import AsyncAgentBay

async def main():
    ab = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    result = await ab.context.list()
    if result.success:
        for ctx in result.contexts:
            print(f'{ctx.name}  |  {ctx.id}')

asyncio.run(main())
"
```

### Standalone 脚本测试
```bash
# 淘宝
poetry run python -m scripts.taobao_link_search SKG --limit 5 --context-key "taobao-context:default:xxx" --output /tmp/taobao_test.json

# 京东
poetry run python -m scripts.jd_link_search SKG --limit 5 --context-key "jd-context:default:xxx" --output /tmp/jd_test.json

# 天猫（用淘宝上下文）
poetry run python -m scripts.tmall_link_search SKG --limit 5 --context-key "taobao-context:default:xxx" --output /tmp/tmall_test.json
```

### 查看提取结果
```bash
cat /tmp/taobao_test.json | python3 -c "
import json,sys
data=json.load(sys.stdin)
for i,item in enumerate(data):
    print(f'Item {i+1}:')
    print(f'  title: {item.get(\"title\",\"\")}')
    print(f'  price: {item.get(\"price\",\"\")}')
    print(f'  sales: {item.get(\"sales\",\"\")}')
    print(f'  shop:  {item.get(\"shop\",\"\")}')
    print(f'  image: {item.get(\"image\",\"\")[:80]}')
    print(f'  url:   {item.get(\"url\",\"\")}')
"
```

### 调试页面 DOM 结构
在脚本中注入 JS 查看页面结构：
```python
info = await page.evaluate('''() => {
    return {
        url: window.location.href,
        title: document.title,
        total_links: document.querySelectorAll("a[href]").length,
        body_preview: document.body.innerText.slice(0, 500),
    }
}''')
```

### 查看所有链接 href
```python
links = await page.evaluate('''() => {
    const links = [];
    for (const a of document.querySelectorAll("a[href]")) {
        const href = a.getAttribute("href") || "";
        if (href.length > 5 && !href.startsWith("#"))
            links.push(href.slice(0, 120));
    }
    return [...new Set(links)].slice(0, 50);
}''')
```

### 查看 SKU 元素（京东专用）
```python
skus = await page.evaluate('''() => {
    return [...document.querySelectorAll("[data-sku]")].map(el => ({
        sku: el.getAttribute("data-sku"),
        html: el.innerHTML.slice(0, 500),
    }));
}''')
```
