from aics.core.tokens import count_tokens


def test_empty() -> None:
    assert count_tokens("") == 0


def test_english_cl100k() -> None:
    assert count_tokens("hello world") == 2


def test_chinese_positive_and_monotonic() -> None:
    a = count_tokens("退货运费由谁承担")
    b = count_tokens("退货运费由谁承担？请说明具体规则。")
    assert 0 < a < b


def test_special_tokens_are_plain_text() -> None:
    assert count_tokens("<|endoftext|>") > 1
