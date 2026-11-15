"""既有 CRUD 用例显式使用测试模拟模式；安全用例另外构造 JWT 配置。"""

import os

os.environ.setdefault("ADMIN_APP_ENV", "test")
os.environ.setdefault("ADMIN_AUTH_MODE", "mock")
