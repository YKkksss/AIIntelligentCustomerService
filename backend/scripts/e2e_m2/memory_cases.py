"""M2.5 long-conversation memory cases (对话上下文管理 §12.2).

Builds `memory_cases.json`: 10 after-sales conversations of 25–60 turns on the M1 corpus
(星河优选). Two kinds of turns:
- filler `f`: scripted user + assistant pair written to the database as-is (the conversation's
  fixed history, like `eval_cases.history`); compaction still runs on them;
- `run`: the real pipeline (RAG → persist → capture_slots → maybe_compact); carries checks:
  `rewrite_any` (any string must appear in rewritten_query), `answer_any` (answered and any
  string in the answer), `forbid` (stale / foreign values that must not appear in the rewrite
  or the answer), `facts` (expected current facts after the turn; None = absent / retracted),
  `tags` (recall / update / pronoun / trap / topic / retract).

Usage: uv run python -m scripts.e2e_m2.memory_cases  (rewrites memory_cases.json)
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

OUT = Path(__file__).with_name("memory_cases.json")

# Scripted after-sales exchanges (assistant text paraphrases the corpus; refusals included).
FILLERS: list[tuple[str, str, str]] = [
    (
        "货到付款有没有金额上限？我想下一单大一点的。",
        "货到付款单笔订单金额上限为 3000 元，超过这个金额的订单需要在线支付。另外部分偏远地区和"
        "大件商品不支持货到付款，下单页会直接提示，您可以在结算页查看当前订单是否支持。",
        "answer",
    ),
    (
        "发票可以开电子的吗？公司报销要用。",
        "可以的，下单后在订单详情页点击“申请发票”即可开具电子普通发票，抬头可以填写公司名称和税号，"
        "一般 24 小时内开具并发送到您的邮箱，电子发票与纸质发票具有同等法律效力，可以用于报销。",
        "answer",
    ),
    (
        "专票怎么申请？需要准备什么？",
        "增值税专用发票需要在订单完成后 90 天内，在“发票中心”提交企业资质申请，包括营业执照、"
        "开户行及账号、注册地址电话等信息。审核通过后专票会邮寄给您，一般 5–7 个工作日送达。",
        "answer",
    ),
    (
        "快递显示签收了但我根本没收到，怎么办？",
        "很抱歉给您带来不便。建议您先联系快递员确认投递位置，也看看是否放在快递柜、驿站或门卫处；"
        "如果 48 小时内仍未找到，可以在订单页提交“未收到货”工单，我们会联系快递公司核实并为您处理。",
        "answer",
    ),
    (
        "退款一般是原路返回吗？多久能到？",
        "是的，退款会原路退回到您的支付账户：余额支付即时到账，支付宝和微信一般 1–3 个工作日，"
        "银行卡一般 1–7 个工作日，具体以银行处理时间为准。退款成功后会有短信通知。",
        "answer",
    ),
    (
        "星河豆是什么东西？怎么获得？",
        "星河豆是星河优选的平台积分，购物确认收货后按实付金额发放，参与签到、评价晒单等活动也能获得。"
        "下单时可以用星河豆抵扣部分金额，具体抵扣比例以最新规则为准。",
        "answer",
    ),
    (
        "星河豆现在能抵多少钱？",
        "按照最新规则，100 星河豆可以抵扣 1 元，单笔订单最多可以抵扣订单金额的 50%。星河豆有有效期，"
        "过期会自动清零，建议您在“我的-星河豆”页面查看即将过期的数量。",
        "answer",
    ),
    (
        "黑卡会员都有哪些权益？值得开吗？",
        "黑卡会员每月可以领取退货运费券，还有专属客服通道、生日礼包和部分商品的会员价。如果您每月"
        "购物和退换货比较频繁，运费券基本就能覆盖会员费用。",
        "answer",
    ),
    (
        "价格保护是什么意思？怎么申请？",
        "价格保护是指您购买后在保价期内商品降价，可以申请退还差价。普通商品保价期为 7 天，大促期间购买"
        "的商品保价期为 30 天，在订单详情页点击“申请价保”即可，差价会以原支付方式或优惠券形式退回。",
        "answer",
    ),
    (
        "同一个商品能申请几次保价？",
        "同一订单中的每件商品最多可以申请 3 次保价，差价以优惠券形式退回的不计入次数。超过次数后系统"
        "将不再受理该商品的保价申请。",
        "answer",
    ),
    (
        "退货的话要我自己寄回去吗？",
        "退货时您可以选择上门取件或者自行寄回。上门取件在提交售后申请时预约时间即可；如果自行寄回，请"
        "保留好运单号，并在 7 天内在售后单中填写，超时未填写售后单可能会被自动关闭。",
        "answer",
    ),
    (
        "运费险是怎么理赔的？",
        "如果下单时购买了运费险或商家赠送了运费险，退货完成后运费险会自动理赔，赔付金额按照首重标准"
        "计算，一般 1–3 个工作日到账，您可以在订单详情中查看理赔进度。",
        "answer",
    ),
    (
        "你们客服几点下班？晚上还能找到人吗？",
        "在线智能客服 7×24 小时为您服务，人工客服的服务时间是每天 9:00–22:00，晚上 22 点以后的问题"
        "可以先留言，人工客服上班后会第一时间回复您。",
        "answer",
    ),
    (
        "不想退了，可以直接换货吗？",
        "支持换货。签收后 15 天内如果商品有质量问题，可以申请换同款同规格的商品；非质量问题的换货请"
        "先申请退货再重新下单。换货寄回运费由我们承担。",
        "answer",
    ),
    (
        "我能修改收货地址吗？填错了。",
        "订单未发货前可以在订单详情页直接修改收货地址，每个订单最多修改 2 次；如果已经发货，需要联系"
        "快递公司申请改派，可能会产生额外的改派费用。",
        "answer",
    ),
    (
        "你们支持比特币支付吗？",
        "抱歉，这个问题我暂时无法回答，您可以换个问法或联系人工客服。",
        "refused",
    ),
    (
        "线下门店周末几点关门？",
        "抱歉，这个问题我暂时无法回答，您可以换个问法或联系人工客服。",
        "refused",
    ),
    ("好的谢谢", "不客气，还有其他问题随时问我。", "answer"),
    ("嗯嗯明白了", "好的，还有什么可以帮您？", "answer"),
    (
        "退款进度在哪里可以看到？",
        "您可以在“我的-售后服务”中查看每个售后单的进度，包括审核、取件、仓库签收、质检和退款等节点，"
        "每个节点完成后都会有短信和站内信通知。",
        "answer",
    ),
    (
        "退货的时候外包装坏了还能退吗？",
        "只要商品本身完好、配件和赠品齐全，外包装轻微破损不影响七天无理由退货。如果包装严重破损影响"
        "二次销售，仓库质检时可能会酌情扣减部分退款。",
        "answer",
    ),
    (
        "买东西送的赠品也要一起退吗？",
        "需要的，赠品需要和商品一起退回。如果赠品缺失或已经使用，会按照赠品的价值从退款中扣减，具体"
        "金额以商品详情页标注的赠品价值为准。",
        "answer",
    ),
    (
        "什么情况下不支持七天无理由退货？",
        "以下商品一般不支持七天无理由退货：定制类商品、鲜活易腐类商品、已激活的数码产品、拆封后影响"
        "人身安全或健康的个人护理用品、在线下载或已拆封的音像制品等，具体以商品页标识为准。",
        "answer",
    ),
    (
        "售后单提交以后多久会审核？",
        "售后申请一般在 24 小时内完成审核，大促期间单量较大，审核时间可能延长到 48 小时。审核结果会"
        "通过短信通知您，您也可以在售后单中随时查看。",
        "answer",
    ),
    (
        "刚才有人打电话说是你们客服，让我提供验证码。",
        "请您千万不要透露验证码！星河优选的客服不会以任何理由索要验证码、银行卡密码或让您转账。如有"
        "疑问请通过 App 内的在线客服或官方渠道核实，必要时可以报警处理。",
        "answer",
    ),
    (
        "约好了上门取件，快递员一直没来。",
        "很抱歉。您可以在售后单中点击“修改取件时间”重新预约，也可以改为自行寄回并上传运单号；如果"
        "多次预约仍未上门，可以提交投诉，我们会督促快递公司处理。",
        "answer",
    ),
    (
        "能开纸质发票吗？",
        "目前默认开具电子普通发票，与纸质发票具有同等效力。如果需要纸质增值税专用发票，需要企业在"
        "发票中心提交资质申请，审核通过后邮寄给您。",
        "answer",
    ),
    (
        "下单之后一般多久发货？",
        "现货商品一般在付款后 48 小时内发货，预售商品以商品页约定的发货时间为准。大促期间订单量大，"
        "发货时间可能会适当延长，您可以在订单详情查看预计发货时间。",
        "answer",
    ),
    (
        "我想取消订单，怎么操作？",
        "未发货的订单可以在订单详情页直接点击“取消订单”，款项会原路退回；如果已经发货，您可以选择"
        "拒收，或者签收后再申请退货。",
        "answer",
    ),
    (
        "你们卖的东西是正品吗？有保障吗？",
        "平台商品均来自品牌授权或官方渠道，入驻商家都经过资质审核，并支持假一赔四。如果您对商品真伪"
        "有疑问，可以提交鉴定申请，我们会安排第三方机构鉴定。",
        "answer",
    ),
]


def fillers(rng: random.Random, n: int) -> list[dict[str, Any]]:
    picks = rng.sample(range(len(FILLERS)), k=min(n, len(FILLERS)))
    while len(picks) < n:
        picks.append(rng.randrange(len(FILLERS)))
    return [
        {"t": "f", "q": FILLERS[i][0], "a": FILLERS[i][1], "type": FILLERS[i][2]} for i in picks
    ]


def heavy_fillers(rng: random.Random, n: int) -> list[dict[str, Any]]:
    """Verbose turns (long complaints, detailed multi-part replies) for the budget stress case."""
    out = []
    answers = [f for f in FILLERS if f[2] == "answer" and len(f[1]) > 60]
    for _ in range(n):
        a, b, c = rng.sample(answers, 3)
        q = (
            f"我再补充一下情况：{a[0]}还有，{b[0]}另外我想确认，{c[0]}"
            "这几个问题都挺急的，麻烦一次性详细说明，我好跟家里人商量。"
        )
        out.append({"t": "f", "q": q, "a": f"{a[1]}\n\n{b[1]}\n\n{c[1]}", "type": "answer"})
    return out


def run(q: str, **checks: Any) -> dict[str, Any]:
    return {"t": "run", "q": q, **checks}


def build() -> list[dict[str, Any]]:
    rng = random.Random(20261016)  # noqa: S311 - deterministic test data, not security
    F = lambda n: fillers(rng, n)  # noqa: E731
    convs: list[dict[str, Any]] = []

    # C01 — clothing return, order number corrected early, pronoun reference late (30 turns)
    convs.append(
        {
            "id": "c01",
            "title": "服装退货：订单号早期更正 + 指代",
            "turns": [
                run(
                    "你好，我上周买了一件羽绒服，订单号 2026100811235，穿着不合适想退。",
                    facts={"order_no": "2026100811235"},
                    tags=["slot"],
                ),
                *F(5),
                run(
                    "不好意思，刚才订单号说错了，应该是 2026100811253。",
                    facts={"order_no": "2026100811253"},
                    tags=["update"],
                ),
                *F(13),
                run(
                    "那件衣服签收后几天内可以无理由退？",
                    rewrite_any=["羽绒服", "衣服", "服装"],
                    answer_any=["15"],
                    tags=["recall", "pronoun"],
                ),
                *F(5),
                run(
                    "我那个订单现在退的话，还在无理由期限内吗？我是 10 月 9 号签收的。",
                    rewrite_any=["2026100811253", "羽绒服", "衣服", "服装"],
                    forbid=["2026100811235"],
                    tags=["update", "pronoun"],
                ),
                run(
                    "好的，那就按这个订单号帮我记一下，谢谢。",
                    forbid=["2026100811235"],
                    facts={"order_no": "2026100811253"},
                    tags=["update"],
                ),
            ],
        }
    )

    # C02 — purifier X5, friend's X7 as a trap, topic switches (40 turns)
    convs.append(
        {
            "id": "c02",
            "title": "净化器 X5：朋友的 X7 干扰 + 话题切换",
            "turns": [
                run(
                    "我买的是星河 X5 空气净化器，用了两个月，想问下保养的事。",
                    facts={"product": "X5"},
                    tags=["slot"],
                ),
                *F(6),
                run(
                    "另外我朋友买的是 X7，他家客厅 40 平，X7 够用吗？",
                    answer_any=["X7"],
                    tags=["topic"],
                ),
                *F(12),
                run(
                    "回到我自己那台，滤网多久换一次？用什么型号的滤网？",
                    rewrite_any=["X5"],
                    answer_any=["F5"],
                    forbid=["X7 的滤网"],
                    tags=["recall", "trap"],
                ),
                *F(10),
                run("屏幕上显示 E07 是什么意思？", answer_any=["E07"], tags=["topic"]),
                *F(5),
                run(
                    "我那台保修期内滤网坏了能免费换吗？",
                    rewrite_any=["X5"],
                    forbid=["X7"],
                    tags=["recall", "trap"],
                ),
                run("行，谢谢你。", tags=["chitchat"]),
            ],
        }
    )

    # C03 — region Xinjiang → moved to Zhejiang (35 turns)
    convs.append(
        {
            "id": "c03",
            "title": "地址更正：新疆 → 浙江",
            "turns": [
                run(
                    "我在新疆乌鲁木齐，网购的东西寄过来一般几天能到？",
                    answer_any=["新疆"],
                    tags=["slot"],
                ),
                *F(9),
                run("对了我上周已经搬到浙江杭州了，以后收货地址都是浙江。", tags=["update"]),
                *F(14),
                run(
                    "那寄到我现在这边首重多少钱？",
                    rewrite_any=["浙江", "杭州"],
                    answer_any=["浙江", "6"],
                    forbid=["新疆"],
                    tags=["update", "pronoun"],
                ),
                *F(6),
                run(
                    "一般几天能到我这儿？",
                    rewrite_any=["浙江", "杭州"],
                    forbid=["新疆", "乌鲁木齐"],
                    tags=["update", "pronoun"],
                ),
                run("好的明白了。", tags=["chitchat"]),
            ],
        }
    )

    # C04 — phone corrected, order number retracted (45 turns)
    convs.append(
        {
            "id": "c04",
            "title": "手机号更正 + 订单号撤回",
            "turns": [
                run(
                    "我的手机号是 13812345678，订单 2026101200777 的面霜用了过敏，怎么退？",
                    answer_any=["诊断证明", "过敏"],
                    facts={"phone": "13812345678", "order_no": "2026101200777"},
                    tags=["slot"],
                ),
                *F(8),
                run(
                    "手机号我换了，新号码是 13987654321，以后联系这个。",
                    facts={"phone": "13987654321"},
                    tags=["update"],
                ),
                *F(8),
                run("等等，那个订单号不对，不是这个，你先不用管它了。", tags=["retract"]),
                *F(14),
                run(
                    "过敏退货需要提供什么材料？",
                    rewrite_any=["面霜", "过敏"],
                    answer_any=["诊断证明"],
                    forbid=["13812345678"],
                    tags=["recall"],
                ),
                *F(8),
                run(
                    "你们会打哪个号码联系我？",
                    rewrite_any=["13987654321"],
                    forbid=["13812345678"],
                    facts={"phone": "13987654321"},
                    tags=["update"],
                ),
            ],
        }
    )

    # C05 — two orders: headphones vs infant formula (25 turns)
    convs.append(
        {
            "id": "c05",
            "title": "两个订单：耳机 vs 奶粉（标识符混淆陷阱）",
            "turns": [
                run(
                    "我有两个订单，耳机那单是 2026100900111，奶粉那单是 2026100900222。",
                    tags=["slot", "trap"],
                ),
                *F(5),
                run(
                    "耳机那单有杂音，能换货吗？",
                    rewrite_any=["耳机", "2026100900111"],
                    forbid=["2026100900222"],
                    tags=["trap"],
                ),
                *F(8),
                run(
                    "奶粉那单开封了还能退吗？",
                    rewrite_any=["奶粉"],
                    answer_any=["奶粉"],
                    forbid=["2026100900111"],
                    tags=["trap", "recall"],
                ),
                *F(6),
                run(
                    "奶粉那个订单号你再跟我确认一下？",
                    rewrite_any=["2026100900222", "奶粉"],
                    forbid=["2026100900111"],
                    tags=["trap", "recall"],
                ),
                run("谢谢。", tags=["chitchat"]),
            ],
        }
    )

    # C06 — 60-turn sofa pickup, early details recalled at the very end
    convs.append(
        {
            "id": "c06",
            "title": "60 轮长对话：沙发上门取件，结尾回忆开头",
            "turns": [
                run(
                    "我要退一张沙发，订单号 2026090100345，是大促期间买的，家离仓库大概 30 公里。",
                    facts={"order_no": "2026090100345"},
                    tags=["slot"],
                ),
                *F(16),
                run(
                    "顺便问下，星河豆第 3 版规则的有效期是多久？",
                    answer_any=["第 3 版", "有效期"],
                    tags=["topic"],
                ),
                *F(18),
                run("书籍七天无理由退货寄回邮费怎么算？", answer_any=["8"], tags=["topic"]),
                *F(19),
                run(
                    "回到最开始那张沙发，上门取件费用是多少？",
                    rewrite_any=["沙发"],
                    answer_any=["20", "50", "公里"],
                    tags=["recall"],
                ),
                run(
                    "还有我最早那个订单是大促买的，价格保护多少天？",
                    rewrite_any=["2026090100345", "沙发", "大促"],
                    answer_any=["30"],
                    tags=["recall"],
                ),
                run("好的，谢谢你这么耐心。", tags=["chitchat"]),
            ],
        }
    )

    # C07 — cross-border order, Hong Kong return shipping, topic switches (50 turns)
    convs.append(
        {
            "id": "c07",
            "title": "跨境订单：话题切换后指代“那个海外订单”",
            "turns": [
                run(
                    "我有一个海外仓直邮的订单 HW2026100566881，想退货。",
                    facts={"order_no": "HW2026100566881"},
                    tags=["slot"],
                ),
                *F(11),
                run(
                    "另外我妈妈在香港，港澳台退货寄回的运费最多报销多少？",
                    answer_any=["60"],
                    tags=["topic"],
                ),
                *F(16),
                run(
                    "刚才那个海外订单的退款多久能到账？",
                    rewrite_any=["海外", "直邮", "HW2026100566881"],
                    answer_any=["15"],
                    tags=["recall", "pronoun"],
                ),
                *F(16),
                run("跨境退货的关税会退吗？", answer_any=["关税", "duties"], tags=["topic"]),
                run(
                    "那就先这样，订单号你别记错了。",
                    forbid=[],
                    facts={"order_no": "HW2026100566881"},
                    tags=["recall"],
                ),
            ],
        }
    )

    # C08 — laptop activation + phone, distractor service hotline in history (30 turns)
    convs.append(
        {
            "id": "c08",
            "title": "笔记本激活 + 历史中的客服热线干扰",
            "turns": [
                run(
                    "我买了台笔记本电脑，订单 2026101000999，已经开机激活了，还能七天无理由吗？",
                    answer_any=["激活"],
                    facts={"order_no": "2026101000999"},
                    tags=["slot"],
                ),
                *F(4),
                {
                    "t": "f",
                    "q": "你们客服电话是多少？",
                    "a": "官方客服热线 4008123123，工作时间 7×24 小时。",
                    "type": "answer",
                },
                *F(10),
                run(
                    "那这台电脑要是有质量问题呢？",
                    rewrite_any=["笔记本", "电脑"],
                    tags=["recall", "pronoun"],
                ),
                *F(9),
                run(
                    "我的订单号你那边记的是多少？",
                    rewrite_any=["2026101000999"],
                    forbid=["4008123123"],
                    tags=["recall", "trap"],
                ),
                run("好，谢谢。", tags=["chitchat"]),
            ],
        }
    )

    # C09 — merchant side: violations, topic switch, pronoun to penalty (40 turns)
    convs.append(
        {
            "id": "c09",
            "title": "商家咨询：扣分规则多次切换",
            "turns": [
                run("我是平台上的商家，店铺 ID 是 SHOP20260088，想了解违规扣分。", tags=["slot"]),
                *F(8),
                run("虚假发货一次扣几分？", answer_any=["6"], tags=["topic"]),
                *F(10),
                run("延迟发货最多要赔顾客多少钱？", answer_any=["500"], tags=["topic"]),
                *F(12),
                run(
                    "那刚才说的虚假发货，累计扣满 12 分会怎样？",
                    rewrite_any=["12 分", "12分", "扣满"],
                    answer_any=["12"],
                    tags=["pronoun"],
                ),
                *F(5),
                run("商品标题最多能写多少字？", answer_any=["60"], tags=["topic"]),
            ],
        }
    )

    # C10 — warehouse contract number, late recall after a long detour (55 turns)
    convs.append(
        {
            "id": "c10",
            "title": "55 轮：仓储合同编号，长绕路后回忆",
            "turns": [
                run(
                    "我们和你们签了仓储合同，编号 XH-WL-2026-0158，想咨询货物丢失赔偿。",
                    answer_any=["100%"],
                    tags=["slot"],
                ),
                *F(14),
                run(
                    "单票赔偿上限是多少？",
                    rewrite_any=["仓储", "合同", "XH-WL-2026-0158"],
                    answer_any=["5,000", "5000"],
                    tags=["recall", "pronoun"],
                ),
                *F(19),
                run("仓储服务费是每月几号结算付款？", answer_any=["15"], tags=["topic"]),
                *F(16),
                run(
                    "我们那份合同的争议由哪里仲裁？",
                    rewrite_any=["XH-WL-2026-0158", "仓储合同"],
                    answer_any=["仲裁委员会"],
                    tags=["recall"],
                ),
                run("好的，感谢。", tags=["chitchat"]),
            ],
        }
    )
    # C11 — budget stress: 60 verbose turns (~12k tokens), early order + region recalled late
    H = lambda n: heavy_fillers(rng, n)  # noqa: E731
    convs.append(
        {
            "id": "c11",
            "title": "60 轮长消息压力用例：早期订单号与地址",
            "turns": [
                run(
                    "你好，我在四川成都，订单 2026092800468 买的电视机签收第二天就黑屏了，想退货。",
                    facts={"order_no": "2026092800468"},
                    tags=["slot"],
                ),
                *H(18),
                run(
                    "订单号我核对了一下，正确的是 2026092800486，之前那个最后两位写反了。",
                    facts={"order_no": "2026092800486"},
                    tags=["update"],
                ),
                *H(20),
                run(
                    "电视这种大件退货，上门取件要我出钱吗？",
                    rewrite_any=["电视"],
                    answer_any=["上门取件"],
                    tags=["recall"],
                ),
                *H(17),
                run(
                    "我那个订单的退款会退到哪里？订单号再帮我确认下。",
                    rewrite_any=["2026092800486"],
                    forbid=["2026092800468"],
                    tags=["update", "recall"],
                ),
                run("寄到我所在的省份一般要几天？", rewrite_any=["四川", "成都"], tags=["recall"]),
            ],
        }
    )
    return convs


def main() -> None:
    convs = build()
    for c in convs:
        c["n_turns"] = len(c["turns"])
        c["n_runs"] = sum(t["t"] == "run" for t in c["turns"])
    OUT.write_text(
        json.dumps(
            {"description": __doc__.splitlines()[0], "conversations": convs},
            ensure_ascii=False,
            indent=1,
        )
    )
    print([(c["id"], c["n_turns"], c["n_runs"]) for c in convs])


if __name__ == "__main__":
    main()
