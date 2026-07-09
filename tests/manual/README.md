# Manual checks

This directory keeps scripts that touch real third-party services or require
local credentials. They are intentionally excluded from pytest collection.

Run them directly when needed, for example:

```bash
.venv/bin/python tests/manual/model_api/check_openai_gateway.py
.venv/bin/python tests/manual/model_api/check_gemini_api.py
.venv/bin/python tests/manual/xueqiu_monitor/smoke.py --offline
```
