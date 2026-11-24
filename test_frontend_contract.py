"""跨层约定：前端在明文 IP（非 secure context）下也不能崩。

crypto.randomUUID() 只在 HTTPS / localhost 存在。手机用 http://IP 访问时，
它 undefined，app.js 的匹配与反馈两处一调用就抛 TypeError，整个 App 停摆。
补丁在 app.js 顶部用 getRandomValues 补一个同形 UUID，仅在原生方法缺失时生效。

这里做静态契约检查：补丁必须在、必须在两处调用之前、不能重新赋值只读的
window.crypto（那也是一次 TypeError）、且 CSP 不允许内联脚本，所以补丁只能
待在 app.js 里。真机与 CDP 的实测结论记在 docs/进度与交接.md。
"""
import re
import unittest
from pathlib import Path

APP = Path(__file__).with_name("app.js").read_text(encoding="utf-8")
HTML = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
SERVER = Path(__file__).with_name("server.py").read_text(encoding="utf-8")


class FrontendContract(unittest.TestCase):
    def test_random_uuid_fallback_exists_before_both_call_sites(self):
        self.assertIn("!globalThis.crypto.randomUUID", APP,
                      "缺少 randomUUID 兜底：明文 IP 下匹配与反馈会崩")
        self.assertIn("getRandomValues", APP, "兜底必须用任何上下文都可用的 getRandomValues")
        fallback = APP.index("!globalThis.crypto.randomUUID")
        # 两处调用：requestId 与读者 sid
        for marker in ("crypto.randomUUID()",):
            calls = [m.start() for m in re.finditer(re.escape(marker), APP)]
            self.assertGreaterEqual(len(calls), 2, "应仍有两处 randomUUID 调用点")
            self.assertTrue(all(c > fallback for c in calls),
                            "兜底必须定义在调用之前")

    def test_fallback_does_not_reassign_readonly_crypto(self):
        # window.crypto 是只读访问器，赋值会在严格模式下抛 TypeError，
        # 导致整段脚本停止执行（页面卡在“正在打开知遇…”）。
        self.assertNotRegex(APP, r"globalThis\.crypto\s*=",
                            "不能给 globalThis.crypto 赋值，只能往现有对象上加方法")

    def test_patch_lives_in_app_js_not_inline_script(self):
        # CSP 是 script-src 'self'，内联脚本会被拦，补丁放 index.html 不生效。
        self.assertIn("script-src 'self'", SERVER)
        self.assertNotRegex(HTML, r"<script(?![^>]*\bsrc=)",
                            "index.html 不应有内联脚本（会被 CSP 拦掉）")
        self.assertIn('<script src="/app.js"', HTML)

    def test_fallback_uuid_shape_is_valid_v4(self):
        # 校验兜底里三个关键位操作：版本位 4、变体位 8-b、16 字节随机。
        self.assertIn("new Uint8Array(16)", APP)
        self.assertIn("0x0f", APP)  # b[6] & 0x0f | 0x40  -> version 4
        self.assertIn("0x40", APP)
        self.assertIn("0x3f", APP)  # b[8] & 0x3f | 0x80  -> variant 8..b
        self.assertIn("0x80", APP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
