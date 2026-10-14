import re


def check_single(answer):
    if answer is None:
        return False

    text = str(answer).strip()
    if not text:
        return False

    # 单选答案文本中常见逗号（中英文）是句内标点，不应据此判定为多选。
    # 仅在出现明显“多段答案”分隔符时，才判定为非单选。
    strong_delimiters = ["\n", "|", "#", "\t", "\r", "、"]
    for sep in strong_delimiters:
        parts = [p.strip() for p in text.split(sep) if p.strip()]
        if len(parts) > 1:
            return False

    return True


def check_multiple(answer):
    _t = cut(answer)
    if _t is not None and len(_t) > 0:
        return True
    return False


# 判断题的通用同义词(内置兜底): 题库返回 true/false、对/错、√/× 都应该能识别,
# 不该依赖用户在配置里把每一种写法都列全。
BUILTIN_TRUE_SYNONYMS = ("true", "t", "yes", "y", "1", "正确", "对", "√", "是")
BUILTIN_FALSE_SYNONYMS = ("false", "f", "no", "n", "0", "错误", "错", "×", "否", "不对", "不正确")
# 只用于"排除题型不符"的中文/符号判断词:
# 英文 true/false 常常本身就是选项文字(如 A. True / B. False), 不能拿它当排除依据,
# 否则单选题的答案 'False' 会被误判成"题型不符"而丢弃, 退化成随机作答。
_CN_TRUE_WORDS = ("正确", "对", "√", "是")
_CN_FALSE_WORDS = ("错误", "错", "×", "否", "不对", "不正确")
_ASCII_WORD_RE = re.compile(r"^[a-z0-9]+$")


def _pool(items, extra) -> set:
    """把配置词表与内置词表并成小写集合(只保留非纯 ASCII 的词, 用于题型排除判断)。"""
    pool = {str(item).strip().lower() for item in (items or []) if str(item).strip()}
    pool |= {str(item).strip().lower() for item in extra}
    return {word for word in pool if word and not _ASCII_WORD_RE.match(word)}


def _judgement_sides(answer, true_list, false_list, include_english: bool) -> int:
    lowered = str(answer).strip().lower()
    if include_english:
        true_pool = {str(x).strip().lower() for x in (true_list or [])} | set(BUILTIN_TRUE_SYNONYMS)
        false_pool = {str(x).strip().lower() for x in (false_list or [])} | set(BUILTIN_FALSE_SYNONYMS)
    else:
        true_pool = _pool(true_list, _CN_TRUE_WORDS)
        false_pool = _pool(false_list, _CN_FALSE_WORDS)
    if lowered in true_pool:
        return 1
    elif lowered in false_pool:
        return 0
    else:
        return -1


def check_judgement(answer, true_list, false_list):
    """判断题答案是否合法: true/false、对/错、√/× 等写法都认(大小写不敏感)。"""
    return _judgement_sides(answer, true_list, false_list, include_english=True)


def check_judgement_words_only(answer, true_list, false_list):
    """答案是否是"判断词"(不含英文同义词)。

    只用于单选题/多选题的排除判断: 题库把判断题的答案(如"正确")混进单选题时应该舍弃,
    但 'True'/'False' 这种可能是选项原文, 不能据此舍弃。
    """
    return _judgement_sides(answer, true_list, false_list, include_english=False)


def check_completion(answer):
    if len(answer) > 0:
        return True
    else:
        return False


def check_answer(answer, type, tiku):  # 只会写小杯代码，这里用个tiku感觉怪怪的，但先这么写着
    if type == 'single':
        if check_single(answer) and check_judgement_words_only(answer, tiku.true_list, tiku.false_list) == -1:
            return True
    elif type == 'multiple':
        if check_multiple(answer) and check_judgement_words_only(answer, tiku.true_list, tiku.false_list) == -1:
            return True
    elif type == 'completion':
        if check_completion(answer):
            return True
    elif type == 'judgement':
        if check_judgement(answer, tiku.true_list, tiku.false_list) != -1:
            return True
    else:  # 未知类型不匹配
        return True
    return False


def cut(answer):
    cut_char = [
        "\n",
        ",",
        "，",
        "|",
        "\r",
        "\t",
        "#",
        "*",
        "-",
        "_",
        "+",
        "@",
        "~",
        "/",
        "\\",
        ".",
        "&",
        " ",
        "、",
    ]
    if answer is None:
        return None

    answer = str(answer)
    for char in cut_char:
        if char not in answer:
            continue
        res = [opt.strip() for opt in answer.split(char) if opt.strip()]
        if res:
            return res
    stripped = answer.strip()
    return [stripped] if stripped else None
