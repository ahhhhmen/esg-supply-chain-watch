"""
Canonical event-key helpers for cross-run deduplication.

The key intentionally models an event family rather than an exact title, so
follow-up coverage with different wording or dates can update the same Notion
page instead of creating duplicates.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


_ENTITY_ALIASES = (
    (re.compile(r"大众|volkswagen|\bvw\b", re.IGNORECASE), "volkswagen"),
    (re.compile(r"通用汽车|general motors|\bgm\b", re.IGNORECASE), "general-motors"),
    (re.compile(r"梅赛德斯|奔驰|mercedes", re.IGNORECASE), "mercedes-benz"),
    (re.compile(r"特斯拉|tesla", re.IGNORECASE), "tesla"),
    (re.compile(r"宝马|\bbmw\b", re.IGNORECASE), "bmw"),
    (re.compile(r"宁德时代|catl", re.IGNORECASE), "catl"),
    (re.compile(r"洛阳钼业|cmoc", re.IGNORECASE), "cmoc"),
    (re.compile(r"青山控股|tsingshan", re.IGNORECASE), "tsingshan"),
)

_SIGNAL_GROUPS = {
    "loc-korea": (r"韩国|korea|korean",),
    "loc-sweden": (r"瑞典|sweden|swedish",),
    "loc-germany": (r"德国|germany|german",),
    "loc-china": (r"中国|china|chinese",),
    "loc-us": (r"美国|美销售|美市场|进入美国|美国市场|u\.s\.|us market|united states|america",),
    "loc-portugal": (r"葡萄牙|portugal",),
    "loc-hungary-debrecen": (r"匈牙利|德布勒森|hungary|debrecen",),
    "loc-congo": (r"刚果|congo|tenke|tfm|kisanfu",),
    "loc-sudan": (r"苏丹|sudan",),
    "actor-if-metall": (r"if metall",),
    "actor-uaw": (r"\buaw\b",),
    "act-strike": (r"罢工|strike|walkout",),
    "act-scale-back": (r"缩减|规模缩减|部分缩减|减少|复工|scaled back|scale back|reduced|reduction|return to work",),
    "act-layoff-restructuring": (
        r"裁员|关厂|关闭.*工厂|关闭|重组|成本削减|layoff|job cuts?|plant closure|factory closure|restructur",
    ),
    "act-ban-restriction": (r"禁令|禁售|禁止|阻止|限制|威胁|ban|bar|block|prohibit|restrict|sales ban",),
    "act-production-cut": (r"削减|减产|产量|工厂产量|闲置|停产|production cut|cuts production|output cut|plant output|idle|shutdown|halt|capacity",),
    "act-recall": (r"召回|recall",),
    "act-review-probe": (r"审查|调查|泄漏|污染|probe|review|investigation|leak|pollution",),
    "act-fire-explosion": (r"起火|火灾|爆炸|fire|explosion",),
    "act-policy-regulation": (r"法规|法案|授权法案|条例|细则|政策|指令|标准|公告|regulation|delegated act|directive|policy|rule|standard|notice|announcement",),
    "act-export-ban": (r"出口禁令|禁运|限制出口|出口管制|两用物项|管控名单|export ban|export control|export restriction|dual-use",),
    "obj-engine": (r"发动机|engine",),
    "obj-vehicle": (r"车辆|车型|汽车|vehicle|car|model",),
    "obj-plant": (r"工厂|plant|factory",),
    "obj-supplier": (r"供应商|supplier",),
    "obj-truck-axle": (r"卡车|车轴|truck|axle",),
    "obj-ownership": (r"股权|持股|资本|ownership|stake|shareholding",),
    "obj-gold-refinery": (r"黄金|精炼厂|gold|refinery",),
    "obj-recycled-content": (r"回收|再生|回收含量|质量平衡|电池护照|recycled content|quality balance|mass balance|battery passport|recycling",),
    "obj-critical-minerals": (r"关键矿产|稀土|锂|钴|镍|石墨|铜|critical minerals|rare earths|lithium|cobalt|nickel|graphite|copper",),
}

_ACTION_PREFIXES = ("act-",)
_ANCHOR_PREFIXES = ("loc-", "obj-")


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _has(text: str, *patterns: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def canonical_entity(entity: str) -> str:
    normalized = _norm(entity)
    for pattern, value in _ENTITY_ALIASES:
        if pattern.search(normalized):
            return value
    tokens = _TOKEN_RE.findall(normalized)
    return "-".join(tokens[:6]) or "unknown"


def _date_bucket(date: str, days: int = 7) -> str:
    """Return a stable bucket so generic fallback keys can merge nearby follow-ups."""
    try:
        dt = datetime.strptime(str(date)[:10], "%Y-%m-%d")
    except ValueError:
        return str(date)[:10]
    anchor = datetime(1970, 1, 5)
    bucket = (dt - anchor).days // days
    start = anchor + timedelta(days=bucket * days)
    return start.strftime("%Y-%m-%d")


def semantic_event_signature(event: dict) -> str:
    """Build a deterministic incident-family signature from stable event facts."""
    entity_key = canonical_entity(str(event.get("entity", "")))
    
    # 优先并聚合所有潜在的标题（不含动态生成的 insight），确保规则判定有充足的输入
    title_zh = _norm(event.get("display_title_zh") or event.get("title") or "")
    title_en = _norm(event.get("core_event_title_en") or event.get("english_title") or "")
    text = " ".join(x for x in (entity_key, title_zh, title_en) if x)

    signals: list[str] = []
    for label, patterns in _SIGNAL_GROUPS.items():
        if _has(text, *patterns):
            signals.append(label)

    actions = sorted(s for s in signals if s.startswith(_ACTION_PREFIXES))
    anchors = sorted(s for s in signals if s.startswith(_ANCHOR_PREFIXES))
    if not actions or not anchors:
        return ""

    signature_parts = actions + anchors
    return f"event:v2:{entity_key}:{_date_bucket(str(event.get('date', '')))}:{':'.join(signature_parts)}"


def canonical_event_key(event: dict) -> str:
    entity = _norm(event.get("entity", ""))
    entity_key = canonical_entity(entity)
    
    # 对规则匹配用到的 text 同样剔除 insight
    title_zh = _norm(event.get("display_title_zh") or event.get("title") or "")
    title_en = _norm(event.get("core_event_title_en") or event.get("english_title") or "")
    text = " ".join(x for x in (entity, title_zh, title_en) if x)

    # 1. 欧盟电池法规 (EU Batteries Regulation 2023/1542 授权法案/质量平衡/回收含量)
    if (_has(entity, r"欧盟|european union|\beu\b") or _has(text, r"欧盟|european union|\beu\b")) and _has(
        text,
        r"2023/1542|电池法规|电池与废电池|batteries regulation|delegated act|授权法案|质量平衡|回收含量|recycled content|quality balance|mass balance",
    ):
        return "event:v1:eu:batteries-regulation-delegated-act-recycled-content"

    # 2. 津巴布韦锂矿出口禁令
    if (_has(entity, r"津巴布韦|zimbabwe") or _has(text, r"津巴布韦|zimbabwe")) and _has(
        text, r"锂|lithium"
    ) and _has(text, r"禁令|出口|ban|export"):
        return "event:v1:zimbabwe:lithium-export-ban"

    # 3. 中国商务部两用物项出口管制公告
    if (_has(entity, r"中国|商务部|mofcom|china") or _has(text, r"商务部|mofcom|23号公告|两用物项")) and _has(
        text, r"两用物项|出口管制|管控名单|dual-use|export control|mp materials"
    ):
        return "event:v1:china-mofcom:dual-use-export-control-list"

    # 4. 美国国防部碳酸锂储备采购
    if (_has(entity, r"美国|defense logistics|department of defense|dod") or _has(text, r"sp8000-26-r-0021|国防后勤局|defense logistics")) and _has(
        text, r"锂|碳酸锂|lithium|stockpile"
    ):
        return "event:v1:us-dod:lithium-carbonate-stockpile-procurement"

    # 5. G7 关键矿产战略联盟
    if (_has(entity, r"g7|七国集团") or _has(text, r"g7|七国集团")) and _has(
        text, r"关键矿产|战略联盟|矿产联盟|critical minerals"
    ):
        return "event:v1:g7:critical-minerals-strategic-alliance"

    # 6. 宝马韩国发动机起火禁售/禁令
    if (_has(entity, r"宝马|bmw") or _has(text, r"宝马|bmw")) and _has(text, r"韩国|korea|south korea") and _has(text, r"起火|火灾|fire|explosion") and _has(text, r"禁令|禁售|sales ban|ban"):
        return "event:v1:bmw:korea-engine-fire-sales-ban"

    if _has(entity, r"通用汽车|general motors|\bgm\b") and _has(text, r"unifor") and _has(
        text, r"工会|谈判|集体|bargaining|union|target"
    ):
        return "event:v1:general-motors:unifor-collective-bargaining"

    if _has(entity, r"梅赛德斯|奔驰|mercedes") and _has(
        text, r"加班|工时|薪酬|无偿|加班费|延长工时|减产|overtime|wage|working hours"
    ) and _has(text, r"德国|员工|工会|germany|workers"):
        return "event:v1:mercedes-benz:germany-working-hours-wage-cut"

    if (_has(entity, r"bhrrc|企业人权|business and human rights") or _has(text, r"bhrrc")) and _has(
        text, r"转型矿产|人权|中国投资|transition minerals|human rights|chinese investment"
    ):
        return "event:v1:bhrrc:transition-minerals-human-rights-report"

    if _has(entity, r"大众|volkswagen|vw") and _has(
        text,
        r"裁员|关厂|关闭.*工厂|德国工厂|工会|layoff|job cuts?|plant closure|factory closure|union",
    ):
        return "event:v1:volkswagen:restructuring-layoffs-germany"

    if _has(entity, r"通用汽车|general motors|\bgm\b") and _has(text, r"\buaw\b|罢工|strike|walkout") and _has(
        text,
        r"卡车|车轴|供应商|工厂|truck|axle|supplier|plant|factory",
    ):
        return "event:v1:general-motors:uaw-truck-supplier-strike"

    if _has(entity, r"通用汽车|general motors|\bgm\b") and _has(
        text,
        r"削减|减产|产量|工厂产量|production cut|cuts production|output cut|plant output",
    ) and _has(text, r"两家|2家|two|工厂|plants?|factories"):
        return "event:v1:general-motors:two-plant-production-cuts"

    if _has(entity, r"梅赛德斯|奔驰|mercedes") and _has(text, r"中国|china|chinese") and _has(
        text,
        r"美国|美销售|美市场|进入美国|美国市场|u\.s\.|us market|united states|america",
    ) and _has(
        text,
        r"禁令|禁止|阻止|限制|威胁|ban|bar|block|prohibit|restrict|sales ban",
    ):
        return "event:v1:mercedes-benz:china-ownership-us-sales-ban"

    if _has(entity, r"梅赛德斯|奔驰|mercedes") and _has(text, r"gls|爆炸|explosion"):
        return "event:v1:mercedes-benz:gls-explosion"

    if _has(entity, r"特斯拉|tesla") and _has(text, r"德国|germany") and _has(
        text,
        r"工厂|产能|闲置|停产|减产|plant|factory|idle|shutdown|halt|capacity",
    ):
        return "event:v1:tesla:germany-plant-underutilization"

    if _has(entity, r"宁德时代|catl") and _has(text, r"匈牙利|德布勒森|hungary|debrecen") and _has(
        text,
        r"审查|调查|泄漏|污染|probe|review|investigation|leak|pollution",
    ):
        return "event:v1:catl:hungary-debrecen-probe"

    if _has(entity, r"洛阳钼业|cmoc") and _has(text, r"tenke|tfm|刚果|congo|kisanfu") and _has(
        text,
        r"罢工|劳资|薪酬|暴露|供应|strike|labor|worker|wage|supply",
    ):
        return "event:v1:cmoc:congo-labor-supply-disruption"

    semantic_key = semantic_event_signature(event)
    if semantic_key:
        return semantic_key

    # 终极 Fallback Token Key 逻辑：为了极致的去重稳定性，
    # 仅使用 canonical entity 与 core_event_title_en（英文核心标题）生成 key。
    # 这样大模型就算微调了 display_title_zh 或生成了不同的分析 insight，External ID 依然完全保持一致。
    title_stable = _norm(
        event.get("core_event_title_en")
        or event.get("display_title_zh")
        or event.get("title")
        or event.get("english_title")
        or ""
    )
    tokens = _TOKEN_RE.findall(f"{entity} {title_stable}")
    compact = "-".join(tokens[:12]) or "unknown"
    return f"event:v1:{entity_key}:{_date_bucket(str(event.get('date', '')))}:{compact}"


def canonical_external_id(event: dict) -> str:
    return hashlib.sha256(canonical_event_key(event).encode("utf-8")).hexdigest()[:12]


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value or ""))


def _is_chinese_language(value: str) -> bool:
    lang = _norm(value)
    return bool(re.search(r"中文|汉语|chinese|\bzh\b|mandarin", lang))


def original_english_title(event: dict) -> str:
    """Return the original non-Chinese source title for Notion, not a translated summary."""
    if _is_chinese_language(str(event.get("original_language", ""))):
        return ""

    candidates = [
        event.get("original_title"),
        event.get("source_title"),
        event.get("raw_title"),
        event.get("article_title"),
        event.get("title_original"),
    ]

    sources = event.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                candidates.extend([
                    source.get("original_title"),
                    source.get("source_title"),
                    source.get("title"),
                    source.get("raw_title"),
                ])

    existing = str(event.get("english_title") or "").strip()
    if existing:
        candidates.append(existing)

    for candidate in candidates:
        title = str(candidate or "").strip()
        if title and not _contains_cjk(title):
            return title
    return ""


def fallback_english_title(event: dict) -> str:
    original = original_english_title(event)
    if original:
        return original

    return "N/A" if event.get("english_title_not_applicable") else ""
