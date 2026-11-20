"""匹配接缝：B2 定契约，B1 换实现。

server.py 只通过 run() 取匹配结果，不关心内部是本地样例还是真实检索。
B1 接知乎接口时，只替换本文件的实现，保持下面的输入输出不变，
服务端、前端和现有测试都不用改。

输入
    project: dict，项目登记内容，键固定为
        name, url, material, task, output, limits, audience
        字段定义见 API.md 与 fixtures.EXAMPLE。
    mode: "synthetic" 或 "live"
        synthetic 用本地样例库，live 表示走真实检索。

输出
    dict，恰好三个键：
        status: "success" | "empty" | "failed"
            success 至少一条候选；empty 检索成功但没有命中；failed 调用失败
        errorCode: None，或下列之一
            "not_configured"  凭据或配置缺失
            "upstream_error"  上游返回异常
            "rate_limited"    额度用尽
            "timeout"         超时
        candidates: list，最多 3 条，按 judgment 排序（适配 → 待核实 → 排除）
            每条为 dict，键固定为
                id, title, author, url, sourceType, evidence,
                judgment（"适配" | "待核实" | "排除"）, reason, unknown

约束
    1. 纯函数：不写数据库、不读请求头、不读环境变量以外的状态。
    2. 失败必须落在 status / errorCode 上，不要抛异常给调用方；
       server.py 捕获到异常会返回 500，前端拿不到可用的错误状态。
    3. 单次调用控制在 10 秒内。当前服务端同步调用，没有超时中断，
       真实检索若可能更久，先按 10 秒上限做请求超时。
    4. 候选里的 url 必须是站内路径（/source/xxx）或完整外链，
       前端直接渲染，不再拼接。
"""
from fixtures import rank

MODE_SYNTHETIC = "synthetic"
MODE_LIVE = "live"
STATUSES = ("success", "empty", "failed")
ERROR_CODES = ("not_configured", "upstream_error", "rate_limited", "timeout")
JUDGMENTS = ("适配", "待核实", "排除")


def run(project, mode=MODE_SYNTHETIC):
    """返回 {status, errorCode, candidates}。B1 替换本函数体即可。"""
    if mode == MODE_LIVE:
        # 真实检索尚未接入。返回明确状态，不要假装成功。
        return dict(status="failed", errorCode="not_configured", candidates=[])
    candidates = rank(project)
    return dict(
        status="success" if candidates else "empty",
        errorCode=None,
        candidates=candidates,
    )
