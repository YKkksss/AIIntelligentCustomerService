import uuid

import jieba
import pytest

from aics.core import textsearch
from aics.core.textsearch import (
    MAX_QUERY_TERMS,
    build_tsquery,
    clear_tokenizer_cache,
    get_tokenizer,
    tokenize_for_index,
)

APP_A = uuid.UUID("00000000-0000-7000-8000-00000000000a")
APP_B = uuid.UUID("00000000-0000-7000-8000-00000000000b")


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    clear_tokenizer_cache()


def test_basic_or_query() -> None:
    q = build_tsquery(APP_A, "我想退货，运费谁承担？")
    assert q is not None
    terms = q.split(" | ")
    assert "'退货'" in terms and "'运费'" in terms and "'承担'" in terms
    assert all(t.startswith("'") and t.endswith("'") for t in terms)


def test_sanitizes_operators_and_quotes() -> None:
    q = build_tsquery(APP_A, "退货 & 运费 | !退款 'x' :* (a) <-> \\ ;drop")
    assert q is not None
    for bad in ["&", "!", "(", ")", "<->", ":*", "\\", ";"]:
        assert bad not in q
    assert q.count("'") % 2 == 0


def test_stopwords_and_single_han_removed() -> None:
    q = build_tsquery(APP_A, "请问我的订单什么时候到")
    assert q is not None
    terms = {t.strip("'") for t in q.split(" | ")}
    assert "请问" not in terms and "我" not in terms and "的" not in terms
    assert "订单" in terms
    assert not any(len(t) == 1 and "\u4e00" <= t <= "\u9fff" for t in terms)


def test_single_alnum_kept_and_lowercased() -> None:
    q = build_tsquery(APP_A, "型号 X 7 MacBook")
    assert q is not None
    terms = q.split(" | ")
    assert "'x'" in terms and "'7'" in terms and "'macbook'" in terms


def test_dedup_and_limit() -> None:
    q = build_tsquery(APP_A, "退货 退货 退货")
    assert q == "'退货'"
    many = " ".join(f"word{i}" for i in range(100))
    q2 = build_tsquery(APP_A, many)
    assert q2 is not None and len(q2.split(" | ")) == MAX_QUERY_TERMS


@pytest.mark.parametrize("text", ["", "   ", "？！。，", "的了吗", "& | !"])
def test_empty_returns_none(text: str) -> None:
    assert build_tsquery(APP_A, text) is None


def test_custom_dict_per_app_isolated() -> None:
    word = "小优会员卡"
    with_dict = build_tsquery(APP_A, "小优会员卡怎么办理", custom_dict=[word])
    without = build_tsquery(APP_B, "小优会员卡怎么办理")
    assert with_dict is not None and f"'{word}'" in with_dict.split(" | ")
    assert without is None or f"'{word}'" not in without.split(" | ")


def test_global_jieba_untouched() -> None:
    word = "极光星云套餐"
    get_tokenizer(APP_A, [word])
    assert word not in jieba.lcut(word)  # 全局词典没有被修改
    assert word in get_tokenizer(APP_A, [word]).lcut(word)


def test_cache_keyed_by_app_and_dict() -> None:
    t1 = get_tokenizer(APP_A, ["甲乙"])
    assert get_tokenizer(APP_A, ["甲乙", " "]) is t1  # 同内容（忽略空白与顺序）命中缓存
    assert get_tokenizer(APP_A, ["丙丁"]) is not t1
    assert get_tokenizer(APP_B, ["甲乙"]) is not t1


def test_cache_lru_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(textsearch, "TOKENIZER_CACHE_SIZE", 2)
    a = get_tokenizer("a")
    get_tokenizer("b")
    get_tokenizer("c")
    assert get_tokenizer("a") is not a


def test_tokenize_for_index() -> None:
    out = tokenize_for_index(APP_A, "我想退货，运费谁承担")
    assert "退货" in out.split() and "  " not in out


def _terms(tsq: str | None) -> set[str]:
    return {t.strip(" '") for t in (tsq or "").split("|")}


def test_question_words_stripped_from_query() -> None:
    from aics.core.textsearch import STOPWORDS

    q = "衣服买了不合适，几天内可以无理由退？退款一般多久到账？"
    new = _terms(build_tsquery(uuid.uuid4(), q))
    assert not new & {"几天", "多久", "可以"}
    assert {"衣服", "理由", "退款"} <= new
    old = _terms(build_tsquery(uuid.uuid4(), q, stopwords=STOPWORDS))
    assert "多久" in old and new < old


def test_english_question_words_stripped() -> None:
    assert _terms(build_tsquery(uuid.uuid4(), "How do I reset my AirPure filter?")) == {
        "reset",
        "airpure",
        "filter",
    }


def test_limit_words_kept_and_custom_dict_overrides_stopword() -> None:
    assert "以内" in _terms(build_tsquery(uuid.uuid4(), "七天以内能退吗"))
    assert "多久" in _terms(build_tsquery(uuid.uuid4(), "多久 保修", ["多久"]))


def test_only_question_words_returns_none() -> None:
    assert build_tsquery(uuid.uuid4(), "请问要多久？怎么办") is None


def test_dropped_phrase_leaks_no_fragments_and_domain_words_kept() -> None:
    assert _terms(build_tsquery(uuid.uuid4(), "专票要在多长时间内申请？")) == {"专票", "申请"}
    # 一般 is a rule category (一般违规 vs 严重违规), so it is not a stopword
    assert "一般" in _terms(build_tsquery(uuid.uuid4(), "一般违规扣满 12 分"))
