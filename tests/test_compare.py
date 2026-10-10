"""Tests for the evaluator comparison rule (harness/compare.py)."""
import compare


def test_json_semantic_equality_ignores_key_order():
    expected = {"status": 200, "body": '{"a":1,"b":2}'}
    observed = {"status": 200, "body": '{"b":2,"a":1}'}
    ok, reason = compare.compare(expected, observed)
    assert ok, reason


def test_json_nested_key_order_ignored():
    a = '{"x":{"p":1,"q":2},"y":[1,2,3]}'
    b = '{"y":[1,2,3],"x":{"q":2,"p":1}}'
    assert compare.bodies_equal(a, b)


def test_array_order_matters():
    assert not compare.bodies_equal('{"y":[1,2,3]}', '{"y":[3,2,1]}')


def test_text_exact_match_required_for_non_json():
    # Neither body is JSON: compared as exact text.
    assert compare.bodies_equal("plain text", "plain text")
    assert not compare.bodies_equal("plain text", "plain text!")


def test_trailing_space_in_json_string_value_is_significant():
    # The real E301 body carries a trailing space in "mensaje".
    with_space = '{"error":"E301","mensaje":"comando desconocido "}'
    without_space = '{"error":"E301","mensaje":"comando desconocido"}'
    assert not compare.bodies_equal(with_space, without_space)
    assert compare.bodies_equal(with_space, with_space)


def test_trailing_space_in_plain_text_is_significant():
    assert not compare.bodies_equal("hello ", "hello")


def test_bool_is_not_equal_to_int():
    assert not compare.bodies_equal('{"ok":true}', '{"ok":1}')
    assert not compare.bodies_equal('{"ok":false}', '{"ok":0}')
    assert compare.bodies_equal('{"ok":true}', '{"ok":true}')


def test_one_json_one_text_falls_back_to_exact_text():
    # One side parses as JSON, the other does not -> exact text comparison.
    assert not compare.bodies_equal('{"a":1}', "not json")


def test_status_mismatch_reported():
    ok, reason = compare.compare(
        {"status": 200, "body": "{}"}, {"status": 404, "body": "{}"}
    )
    assert not ok
    assert "status" in reason


def test_body_mismatch_reported():
    ok, reason = compare.compare(
        {"status": 200, "body": '{"a":1}'}, {"status": 200, "body": '{"a":2}'}
    )
    assert not ok
    assert "status" not in reason  # it's a body difference, not a status one


def test_normalize_classifies_json_and_text():
    kind, value = compare.normalize('{"a":1}')
    assert kind == "json" and value == {"a": 1}
    kind, value = compare.normalize("hello")
    assert kind == "text" and value == "hello"


def test_identical_json_numbers_equal():
    assert compare.bodies_equal('{"saldo":0}', '{"saldo":0}')
    assert not compare.bodies_equal('{"saldo":0}', '{"saldo":1}')
